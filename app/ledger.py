"""谱系台账核心逻辑：所有体积运算一律使用 Decimal，不用 float。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

# 统一量化到小数点后 9 位，避免 1/3 这类无限循环小数在库中无限膨胀。
QUANTUM = Decimal("0.000000001")
ZERO = Decimal("0")


class InsufficientVolume(Exception):
    """某一步吸取体积超过来源孔当前体积，触发整批回滚。"""

    def __init__(self, well: str, available: Decimal, requested: Decimal) -> None:
        self.well = well
        self.available = available
        self.requested = requested
        super().__init__(f"孔位 {well} 体积不足：可用 {available}，请求吸取 {requested}")


def q(value: Decimal) -> Decimal:
    """量化到统一精度。quantize 单调不减，组分量不会被放大到超过原值。"""
    return value.quantize(QUANTUM, rounding=ROUND_HALF_UP)


def dec_str(value: Decimal) -> str:
    """Decimal -> 紧凑字符串（去掉尾随 0，避免科学计数法）。"""
    return format(value.normalize(), "f")


# ---------------------------------------------------------------- 孔位与组成

def ensure_sample(conn: sqlite3.Connection, name: str) -> int:
    conn.execute("INSERT OR IGNORE INTO samples (name) VALUES (?)", (name,))
    row = conn.execute("SELECT id FROM samples WHERE name = ?", (name,)).fetchone()
    return int(row["id"])


def ensure_well(conn: sqlite3.Connection, well: str) -> None:
    conn.execute("INSERT OR IGNORE INTO wells (well, volume) VALUES (?, '0')", (well,))


def get_well_volume(conn: sqlite3.Connection, well: str) -> Decimal | None:
    row = conn.execute("SELECT volume FROM wells WHERE well = ?", (well,)).fetchone()
    return None if row is None else Decimal(row["volume"])


def _composition_amount(conn: sqlite3.Connection, well: str, sample_id: int) -> Decimal:
    row = conn.execute(
        "SELECT amount FROM well_composition WHERE well = ? AND sample_id = ?",
        (well, sample_id),
    ).fetchone()
    return Decimal(row["amount"]) if row else ZERO


def _set_composition(conn: sqlite3.Connection, well: str, sample_id: int, amount: Decimal) -> None:
    if amount == 0:
        conn.execute(
            "DELETE FROM well_composition WHERE well = ? AND sample_id = ?",
            (well, sample_id),
        )
    else:
        conn.execute(
            "INSERT INTO well_composition (well, sample_id, amount) VALUES (?, ?, ?) "
            "ON CONFLICT(well, sample_id) DO UPDATE SET amount = excluded.amount",
            (well, sample_id, dec_str(amount)),
        )


def _set_volume(conn: sqlite3.Connection, well: str, volume: Decimal) -> None:
    conn.execute("UPDATE wells SET volume = ? WHERE well = ?", (dec_str(volume), well))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_event(
    conn: sqlite3.Connection,
    well: str,
    event_type: str,
    *,
    batch_id: int | None = None,
    sample: str | None = None,
    volume: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO well_events (well, type, batch_id, sample, volume, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (well, event_type, batch_id, sample, volume, _now()),
    )


def register_initial_sample(conn: sqlite3.Connection, well: str, sample: str, volume: Decimal) -> None:
    """登记初始样本；孔位已有液体时视为补液并立即混匀。"""
    ensure_well(conn, well)
    sample_id = ensure_sample(conn, sample)
    _set_volume(conn, well, q(get_well_volume(conn, well) + volume))
    _set_composition(conn, well, sample_id, q(_composition_amount(conn, well, sample_id) + volume))
    _record_event(conn, well, "top_up", sample=sample, volume=dec_str(q(volume)))


def _allocate_move(
    amounts: list[tuple[int, Decimal]], move_units: int
) -> dict[int, int]:
    """按比例把 move_units 个最小计量单位（QUANTUM）分配给来源孔的各样本。

    先取比例份额 amount_i * move_units / total 的整数单位，再把剩余单位
    按小数余量从大到小分配；余量相同时按样本 ID 升序。各样本分得的单位数
    必然不超过其原有单位数：若 floor(x_i) 已等于 units_i，则 x_i 是整数，
    余量为 0，不会再分到额外单位。
    """
    total_units = sum(int(amount / QUANTUM) for _, amount in amounts)
    scaled = [
        (sample_id, int(amount / QUANTUM) * move_units) for sample_id, amount in amounts
    ]
    allocated = {sample_id: value // total_units for sample_id, value in scaled}
    remaining = move_units - sum(allocated.values())
    if remaining:
        ordered = sorted(scaled, key=lambda item: (-(item[1] % total_units), item[0]))
        for sample_id, _ in ordered[:remaining]:
            allocated[sample_id] += 1
    return allocated


def apply_transfer(conn: sqlite3.Connection, source: str, destination: str, volume: Decimal) -> None:
    """执行一次移液：按来源组成占比，以最小计量单位（QUANTUM）整数取走各样本。

    用最大余量法分配整数单位，保证实际带走的组分量之和恰好等于请求体积，
    从而来源孔和目标孔在每一步都满足「组分量之和 = 体积」。
    """
    volume = q(volume)
    src_volume = get_well_volume(conn, source) or ZERO
    if src_volume < volume:
        raise InsufficientVolume(source, src_volume, volume)

    ensure_well(conn, destination)
    rows = conn.execute(
        "SELECT sample_id, amount FROM well_composition WHERE well = ? ORDER BY sample_id",
        (source,),
    ).fetchall()
    amounts = [(row["sample_id"], Decimal(row["amount"])) for row in rows]

    move_units = int(volume / QUANTUM)
    moved_units = _allocate_move(amounts, move_units) if move_units else {}

    moved_total = ZERO
    for sample_id, amount in amounts:
        moved = QUANTUM * moved_units.get(sample_id, 0)
        moved_total += moved
        _set_composition(conn, source, sample_id, amount - moved)
        if moved:
            _set_composition(
                conn,
                destination,
                sample_id,
                _composition_amount(conn, destination, sample_id) + moved,
            )

    _set_volume(conn, source, src_volume - moved_total)
    _set_volume(conn, destination, get_well_volume(conn, destination) + moved_total)


def well_state(conn: sqlite3.Connection, well: str) -> dict | None:
    """孔位当前体积 + 各初始样本的量与占比。"""
    volume = get_well_volume(conn, well)
    if volume is None:
        return None
    rows = conn.execute(
        "SELECT s.name AS sample, wc.amount AS amount "
        "FROM well_composition wc JOIN samples s ON s.id = wc.sample_id "
        "WHERE wc.well = ? ORDER BY s.name",
        (well,),
    ).fetchall()
    composition = []
    for row in rows:
        amount = Decimal(row["amount"])
        fraction = q(amount / volume) if volume > 0 else ZERO
        composition.append(
            {"sample": row["sample"], "amount": dec_str(amount), "fraction": dec_str(fraction)}
        )
    return {"well": well, "volume": dec_str(volume), "composition": composition}


# ---------------------------------------------------------------- 批次

def find_batch_by_key(conn: sqlite3.Connection, request_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM batches WHERE request_key = ?", (request_key,)).fetchone()


def insert_batch(conn: sqlite3.Connection, request_key: str, status: str, error: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO batches (request_key, status, error, created_at) VALUES (?, ?, ?, ?)",
        (request_key, status, error, _now()),
    )
    return int(cur.lastrowid)


def _save_pre_batch_snapshot(conn: sqlite3.Connection, batch_id: int, wells: list[str]) -> None:
    """保存该批次首次触及各孔在任何移液发生前的完整快照（体积 + 各样本量）。"""
    for well in wells:
        row = conn.execute("SELECT volume FROM wells WHERE well = ?", (well,)).fetchone()
        volume = None if row is None else row["volume"]
        conn.execute(
            "INSERT INTO batch_well_snapshots (batch_id, well, volume) VALUES (?, ?, ?)",
            (batch_id, well, volume),
        )
        comp_rows = conn.execute(
            "SELECT sample_id, amount FROM well_composition WHERE well = ?",
            (well,),
        ).fetchall()
        conn.executemany(
            "INSERT INTO batch_snapshot_composition (batch_id, well, sample_id, amount) "
            "VALUES (?, ?, ?, ?)",
            [(batch_id, well, r["sample_id"], r["amount"]) for r in comp_rows],
        )


def execute_batch(conn: sqlite3.Connection, request_key: str, operations: list) -> int:
    """在调用方的事务内按提交顺序执行；任一步抛 InsufficientVolume 由调用方回滚。"""
    batch_id = insert_batch(conn, request_key, "committed")
    touched_wells = list(dict.fromkeys(
        well
        for op in operations
        for well in (op.source, op.destination)
    ))
    # 先于任何移液保存批前快照，保证与最终提交同事务、可整体回滚。
    _save_pre_batch_snapshot(conn, batch_id, touched_wells)
    for step, op in enumerate(operations, start=1):
        volume = q(op.volume)
        apply_transfer(conn, op.source, op.destination, volume)
        conn.execute(
            "INSERT INTO operations (batch_id, step, source, destination, volume) "
            "VALUES (?, ?, ?, ?, ?)",
            (batch_id, step, op.source, op.destination, dec_str(volume)),
        )
    for well in touched_wells:
        _record_event(conn, well, "batch", batch_id=batch_id)
    return batch_id


def batch_payload(conn: sqlite3.Connection, batch_id: int, replayed: bool = False) -> dict:
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    ops = conn.execute(
        "SELECT step, source, destination, volume FROM operations "
        "WHERE batch_id = ? ORDER BY step",
        (batch_id,),
    ).fetchall()
    reversal = conn.execute(
        "SELECT reversed_at FROM reversals WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    return {
        "batch_id": row["id"],
        "request_key": row["request_key"],
        "status": row["status"],
        "error": row["error"],
        "created_at": row["created_at"],
        "replayed": replayed,
        "reversed": reversal is not None,
        "reversed_at": reversal["reversed_at"] if reversal else None,
        "operations": [dict(op) for op in ops],
    }


# ---------------------------------------------------------------- 批次撤销

class ReversalConflict(Exception):
    """批次当前不可撤销（已撤销/无快照/存在后续阻塞操作）。"""

    def __init__(self, reason: str, message: str, blockers: list[dict] | None = None) -> None:
        self.reason = reason
        self.message = message
        self.blockers = blockers or []
        super().__init__(message)


def find_reversal_by_key(conn: sqlite3.Connection, request_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM reversals WHERE request_key = ?", (request_key,)
    ).fetchone()


def get_reversal(conn: sqlite3.Connection, reversal_id: int, replayed: bool = False) -> dict | None:
    row = conn.execute(
        "SELECT * FROM reversals WHERE id = ?", (reversal_id,)
    ).fetchone()
    if row is None:
        return None
    wells = [
        r["well"]
        for r in conn.execute(
            "SELECT well FROM batch_well_snapshots WHERE batch_id = ? ORDER BY well",
            (row["batch_id"],),
        ).fetchall()
    ]
    return {
        "reversal_id": row["id"],
        "batch_id": row["batch_id"],
        "request_key": row["request_key"],
        "reversed_at": row["reversed_at"],
        "replayed": replayed,
        "restored_wells": wells,
        "batch": batch_payload(conn, row["batch_id"]),
    }


def _batch_blockers(conn: sqlite3.Connection, batch_id: int, wells: list[str]) -> list[dict]:
    """批次之后触及这些孔的补液（top_up）与已提交批次（batch）事件。

    阻塞基准是「该批次在这个孔上的事件 id」——批次自身在其他孔的事件
    id 可能更大，但不算后续操作。
    """
    blockers: list[dict] = []
    for well in wells:
        marker = conn.execute(
            "SELECT id FROM well_events WHERE type = 'batch' AND batch_id = ? AND well = ?",
            (batch_id, well),
        ).fetchone()
        if marker is None:
            continue
        rows = conn.execute(
            "SELECT e.type AS type, e.batch_id AS blocker_batch_id, b.request_key AS request_key, "
            "       e.sample AS sample, e.volume AS volume "
            "FROM well_events e LEFT JOIN batches b ON b.id = e.batch_id "
            "WHERE e.well = ? AND e.id > ? ORDER BY e.id",
            (well, marker["id"]),
        ).fetchall()
        if not rows:
            continue
        events = []
        for r in rows:
            event: dict = {"type": r["type"]}
            if r["type"] == "top_up":
                event["sample"] = r["sample"]
                event["volume"] = r["volume"]
            else:
                event["batch_id"] = r["blocker_batch_id"]
                event["request_key"] = r["request_key"]
            events.append(event)
        blockers.append({"well": well, "events": events})
    return blockers


def _restore_snapshot(conn: sqlite3.Connection, batch_id: int) -> None:
    """把该批次快照中的每个孔原子恢复到批前状态（含删除批次中新出现的孔）。"""
    snaps = conn.execute(
        "SELECT well, volume FROM batch_well_snapshots WHERE batch_id = ? ORDER BY well",
        (batch_id,),
    ).fetchall()
    for snap in snaps:
        well = snap["well"]
        # 先清组成再动孔，遵守外键约束。
        conn.execute("DELETE FROM well_composition WHERE well = ?", (well,))
        if snap["volume"] is None:
            conn.execute("DELETE FROM wells WHERE well = ?", (well,))
            continue
        conn.execute("UPDATE wells SET volume = ? WHERE well = ?", (snap["volume"], well))
        comp_rows = conn.execute(
            "SELECT sample_id, amount FROM batch_snapshot_composition "
            "WHERE batch_id = ? AND well = ? ORDER BY sample_id",
            (batch_id, well),
        ).fetchall()
        conn.executemany(
            "INSERT INTO well_composition (well, sample_id, amount) VALUES (?, ?, ?)",
            [(well, r["sample_id"], r["amount"]) for r in comp_rows],
        )


def apply_reversal(conn: sqlite3.Connection, batch_id: int, request_key: str) -> int:
    """在调用方事务内完成可撤销性校验、快照恢复与审计记录写入。

    校验不通过抛 ReversalConflict（由调用方随事务回滚，液体状态不变）。
    """
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise ReversalConflict("not_found", f"批次 {batch_id} 不存在")
    if row["status"] != "committed":
        raise ReversalConflict(
            "not_committed",
            f"批次 {batch_id} 状态为 {row['status']}，仅已提交批次可撤销",
        )
    snaps = conn.execute(
        "SELECT well FROM batch_well_snapshots WHERE batch_id = ? ORDER BY well",
        (batch_id,),
    ).fetchall()
    if not snaps:
        raise ReversalConflict(
            "no_snapshot",
            f"批次 {batch_id} 是上线前提交的批次，没有批前快照，不可撤销",
        )
    if conn.execute(
        "SELECT 1 FROM reversals WHERE batch_id = ?", (batch_id,)
    ).fetchone():
        raise ReversalConflict("already_reversed", f"批次 {batch_id} 已经撤销，不能重复撤销")
    wells = [r["well"] for r in snaps]
    blockers = _batch_blockers(conn, batch_id, wells)
    if blockers:
        raise ReversalConflict(
            "blocked",
            f"批次 {batch_id} 之后有补液或后续批次触及相关孔位，不可撤销",
            blockers,
        )

    _restore_snapshot(conn, batch_id)
    cur = conn.execute(
        "INSERT INTO reversals (batch_id, request_key, reversed_at) VALUES (?, ?, ?)",
        (batch_id, request_key, _now()),
    )
    return int(cur.lastrowid)
