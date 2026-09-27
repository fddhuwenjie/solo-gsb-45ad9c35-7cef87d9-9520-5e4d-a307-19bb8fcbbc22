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


def register_initial_sample(conn: sqlite3.Connection, well: str, sample: str, volume: Decimal) -> None:
    """登记初始样本；孔位已有液体时视为补液并立即混匀。"""
    ensure_well(conn, well)
    sample_id = ensure_sample(conn, sample)
    _set_volume(conn, well, q(get_well_volume(conn, well) + volume))
    _set_composition(conn, well, sample_id, q(_composition_amount(conn, well, sample_id) + volume))


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
        (request_key, status, error, datetime.now(timezone.utc).isoformat()),
    )
    return int(cur.lastrowid)


def execute_batch(conn: sqlite3.Connection, request_key: str, operations: list) -> int:
    """在调用方的事务内按提交顺序执行；任一步抛 InsufficientVolume 由调用方回滚。"""
    batch_id = insert_batch(conn, request_key, "committed")
    for step, op in enumerate(operations, start=1):
        volume = q(op.volume)
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
    return {
        "batch_id": row["id"],
        "request_key": row["request_key"],
        "status": row["status"],
        "error": row["error"],
        "created_at": row["created_at"],
        "replayed": replayed,
        "operations": [dict(op) for op in ops],
    }
