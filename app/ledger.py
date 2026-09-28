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


class ReversalConflict(Exception):
    """批次当前不满足撤销前提。code 标识具体冲突，detail 携带阻塞信息。"""

    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        self.code = code
        self.detail = detail or {}
        super().__init__(message)


def q(value: Decimal) -> Decimal:
    """量化到统一精度。quantize 单调不减，组分量不会被放大到超过原值。"""
    return value.quantize(QUANTUM, rounding=ROUND_HALF_UP)


def dec_str(value: Decimal) -> str:
    """Decimal -> 紧凑字符串（去掉尾随 0，避免科学计数法）。"""
    return format(value.normalize(), "f")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def register_initial_sample(conn: sqlite3.Connection, well: str, sample: str, volume: Decimal) -> None:
    """登记初始样本；孔位已有液体时视为补液并立即混匀。"""
    ensure_well(conn, well)
    sample_id = ensure_sample(conn, sample)
    _set_volume(conn, well, q(get_well_volume(conn, well) + volume))
    _set_composition(conn, well, sample_id, q(_composition_amount(conn, well, sample_id) + volume))


def _last_committed_batch_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT MAX(id) AS id FROM batches WHERE status = 'committed'"
    ).fetchone()
    return None if row is None or row["id"] is None else int(row["id"])


def record_registration(conn: sqlite3.Connection, well: str, sample: str, volume: Decimal) -> None:
    """写入补液审计：记录本次登记发生时已提交批次的最大 id。

    必须在一个写事务内、紧邻 register_initial_sample 调用，
    保证该值对「之后提交的批次」严格有序（撤销检查据此判断补液先后）。
    """
    conn.execute(
        "INSERT INTO registrations (well, sample, volume, after_batch_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (well, sample, dec_str(q(volume)), _last_committed_batch_id(conn), _now()),
    )


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


def _snapshot_well(conn: sqlite3.Connection, batch_id: int, well: str) -> None:
    """保存某孔此刻的完整状态作为批前快照；同一批次每孔只存首次触及前的状态。"""
    existing = conn.execute(
        "SELECT 1 FROM batch_well_snapshots WHERE batch_id = ? AND well = ?",
        (batch_id, well),
    ).fetchone()
    if existing is not None:
        return
    volume = get_well_volume(conn, well)
    conn.execute(
        "INSERT INTO batch_well_snapshots (batch_id, well, existed, volume) "
        "VALUES (?, ?, ?, ?)",
        (batch_id, well, 0 if volume is None else 1, dec_str(volume if volume is not None else ZERO)),
    )
    if volume is not None:
        rows = conn.execute(
            "SELECT sample_id, amount FROM well_composition WHERE well = ?",
            (well,),
        ).fetchall()
        conn.executemany(
            "INSERT INTO snapshot_composition (batch_id, well, sample_id, amount) "
            "VALUES (?, ?, ?, ?)",
            [(batch_id, well, row["sample_id"], row["amount"]) for row in rows],
        )


def execute_batch(conn: sqlite3.Connection, request_key: str, operations: list) -> int:
    """在调用方的事务内按提交顺序执行；任一步抛 InsufficientVolume 由调用方回滚。

    每一步执行前，先在同一事务内保存其来源/目标孔的批前快照（每孔仅首次触及前一份），
    这样批次一旦提交，所有触及孔的完整批前状态即随该批次原子落库。
    """
    batch_id = insert_batch(conn, request_key, "committed")
    for step, op in enumerate(operations, start=1):
        volume = q(op.volume)
        _snapshot_well(conn, batch_id, op.source)
        _snapshot_well(conn, batch_id, op.destination)
        apply_transfer(conn, op.source, op.destination, volume)
        conn.execute(
            "INSERT INTO operations (batch_id, step, source, destination, volume) "
            "VALUES (?, ?, ?, ?, ?)",
            (batch_id, step, op.source, op.destination, dec_str(volume)),
        )
    return batch_id


def batch_payload(conn: sqlite3.Connection, batch_id: int, replayed: bool = False) -> dict:
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    ops = conn.execute(
        "SELECT step, source, destination, volume FROM operations "
        "WHERE batch_id = ? ORDER BY step",
        (batch_id,),
    ).fetchall()
    reversal_row = conn.execute(
        "SELECT id, request_key, created_at FROM reversals WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    reversal = None
    if reversal_row is not None:
        reversal = {
            "reversal_id": reversal_row["id"],
            "request_key": reversal_row["request_key"],
            "created_at": reversal_row["created_at"],
        }
    return {
        "batch_id": row["id"],
        "request_key": row["request_key"],
        "status": row["status"],
        "error": row["error"],
        "created_at": row["created_at"],
        "replayed": replayed,
        "reversed": reversal_row is not None,
        "reversal": reversal,
        "operations": [dict(op) for op in ops],
    }


# ---------------------------------------------------------------- 批次撤销

def find_reversal_by_key(conn: sqlite3.Connection, request_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM reversals WHERE request_key = ?", (request_key,)
    ).fetchone()


def reversal_of_batch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM reversals WHERE batch_id = ?", (batch_id,)
    ).fetchone()


def _touched_wells(conn: sqlite3.Connection, batch_id: int) -> list[str]:
    """批次触及的全部孔位（去重，按孔位名排序，保证响应稳定）。"""
    rows = conn.execute(
        "SELECT DISTINCT well FROM ("
        "  SELECT source AS well FROM operations WHERE batch_id = ? "
        "  UNION "
        "  SELECT destination AS well FROM operations WHERE batch_id = ?"
        ") ORDER BY well",
        (batch_id, batch_id),
    ).fetchall()
    return [row["well"] for row in rows]


def _later_blockers_for_well(
    conn: sqlite3.Connection, batch_id: int, well: str
) -> list[dict]:
    """该孔在目标批次之后发生的操作：后续已提交批次（按时间排序）与补液。

    后续批次中已被撤销的不算阻塞——它们的液体效果已通过自身快照原子回滚。
    """
    blockers: list[dict] = []
    batch_rows = conn.execute(
        "SELECT b.id AS id, b.request_key AS request_key, b.created_at AS created_at, "
        "       o.step AS step, o.source AS source, o.destination AS destination, o.volume AS volume "
        "FROM operations o JOIN batches b ON b.id = o.batch_id "
        "WHERE b.status = 'committed' AND b.id > ? "
        "  AND (o.source = ? OR o.destination = ?) "
        "  AND NOT EXISTS (SELECT 1 FROM reversals r WHERE r.batch_id = b.id) "
        "ORDER BY b.created_at, b.id, o.step",
        (batch_id, well, well),
    ).fetchall()
    for row in batch_rows:
        blockers.append(
            {
                "type": "batch",
                "created_at": row["created_at"],
                "batch_id": row["id"],
                "request_key": row["request_key"],
                "operation": {
                    "step": row["step"],
                    "source": row["source"],
                    "destination": row["destination"],
                    "volume": row["volume"],
                },
            }
        )
    reg_rows = conn.execute(
        "SELECT id, sample, volume, created_at FROM registrations "
        "WHERE well = ? AND after_batch_id >= ? "
        "ORDER BY created_at, id",
        (well, batch_id),
    ).fetchall()
    for row in reg_rows:
        blockers.append(
            {
                "type": "top_up",
                "created_at": row["created_at"],
                "registration_id": row["id"],
                "sample": row["sample"],
                "volume": row["volume"],
            }
        )
    blockers.sort(key=lambda item: (item["created_at"], 0 if item["type"] == "batch" else 1))
    return blockers


def _reversal_blockers(conn: sqlite3.Connection, batch_id: int) -> list[dict]:
    """逐孔列出阻塞撤销的后续操作；只返回确实有阻塞的孔。"""
    blocked = []
    for well in _touched_wells(conn, batch_id):
        blockers = _later_blockers_for_well(conn, batch_id, well)
        if blockers:
            blocked.append({"well": well, "blockers": blockers})
    return blocked


def _restore_snapshot(conn: sqlite3.Connection, batch_id: int, well: str) -> None:
    """把单个孔恢复到批前快照：不存在的孔整孔删除，存在的孔覆盖体积与组成。"""
    snapshot = conn.execute(
        "SELECT existed, volume FROM batch_well_snapshots WHERE batch_id = ? AND well = ?",
        (batch_id, well),
    ).fetchone()
    if snapshot is None:
        # 理论上不可达：触及孔都在提交同事务存过快照；失败则保守报错
        raise ReversalConflict(
            "snapshot_missing", f"批次 {batch_id} 缺少孔 {well} 的批前快照，不可撤销"
        )
    if not snapshot["existed"]:
        conn.execute("DELETE FROM well_composition WHERE well = ?", (well,))
        conn.execute("DELETE FROM wells WHERE well = ?", (well,))
        return
    conn.execute("DELETE FROM well_composition WHERE well = ?", (well,))
    rows = conn.execute(
        "SELECT sample_id, amount FROM snapshot_composition WHERE batch_id = ? AND well = ?",
        (batch_id, well),
    ).fetchall()
    ensure_well(conn, well)
    conn.executemany(
        "INSERT INTO well_composition (well, sample_id, amount) VALUES (?, ?, ?)",
        [(well, row["sample_id"], row["amount"]) for row in rows],
    )
    conn.execute(
        "UPDATE wells SET volume = ? WHERE well = ?", (snapshot["volume"], well)
    )


def execute_reversal(conn: sqlite3.Connection, batch_id: int, request_key: str) -> int:
    """在调用方的写事务内原子撤销目标批次。

    前提（任一不满足抛 ReversalConflict，不写任何液体状态）：
      * 批次存在且 status=committed；
      * 尚未被撤销；
      * 拥有完整批前快照（上线前的老批次没有，明确不可撤销）；
      * 触及的每个孔之后都没有补液，也没有仍生效的已提交批次。
    满足时逐孔恢复快照，并写入关联原批次的审计记录；原批次保留不动。
    """
    # 此时调用方已持有写锁（BEGIN IMMEDIATE），以下读取均基于最新已提交状态，
    # 并发同键请求会在这里看到先到者的撤销记录并走重放，而不是重复执行。
    same_key = find_reversal_by_key(conn, request_key)
    if same_key is not None:
        if same_key["batch_id"] != batch_id:
            raise ReversalConflict(
                "request_key_target_mismatch",
                f"request_key {request_key} 已用于撤销批次 {same_key['batch_id']}",
            )
        raise ReversalConflict(
            "already_replayed", f"request_key {request_key} 的撤销已完成"
        )

    row = conn.execute(
        "SELECT id, status FROM batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if row is None:
        raise ReversalConflict("batch_not_found", f"批次 {batch_id} 不存在")
    if row["status"] != "committed":
        raise ReversalConflict(
            "batch_not_committed",
            f"批次 {batch_id} 状态为 {row['status']}，只有已提交批次可撤销",
        )
    if reversal_of_batch(conn, batch_id) is not None:
        raise ReversalConflict(
            "already_reversed", f"批次 {batch_id} 已撤销，不能重复撤销"
        )

    snapshot_rows = conn.execute(
        "SELECT 1 FROM batch_well_snapshots WHERE batch_id = ? LIMIT 1",
        (batch_id,),
    ).fetchall()
    if not snapshot_rows:
        raise ReversalConflict(
            "snapshot_missing",
            f"批次 {batch_id} 是上线快照功能前提交的批次，没有批前快照，不可撤销",
        )

    blocked = _reversal_blockers(conn, batch_id)
    if blocked:
        raise ReversalConflict(
            "blocked",
            f"批次 {batch_id} 之后有孔位被补液或被后续已提交批次改动，不能撤销",
            {"blocked_wells": blocked},
        )

    touched = _touched_wells(conn, batch_id)
    for well in touched:
        _restore_snapshot(conn, batch_id, well)

    cur = conn.execute(
        "INSERT INTO reversals (request_key, batch_id, created_at) VALUES (?, ?, ?)",
        (request_key, batch_id, _now()),
    )
    return int(cur.lastrowid)


def reversal_payload(
    conn: sqlite3.Connection,
    reversal_id: int,
    batch_id: int,
    replayed: bool = False,
) -> dict:
    row = conn.execute("SELECT * FROM reversals WHERE id = ?", (reversal_id,)).fetchone()
    wells = conn.execute(
        "SELECT well, existed, volume FROM batch_well_snapshots WHERE batch_id = ? ORDER BY well",
        (batch_id,),
    ).fetchall()
    return {
        "reversal_id": row["id"],
        "request_key": row["request_key"],
        "batch_id": batch_id,
        "created_at": row["created_at"],
        "replayed": replayed,
        "restored_wells": [
            {"well": well_row["well"], "volume": well_row["volume"], "existed": bool(well_row["existed"])}
            for well_row in wells
        ],
        "batch": batch_payload(conn, batch_id),
    }
