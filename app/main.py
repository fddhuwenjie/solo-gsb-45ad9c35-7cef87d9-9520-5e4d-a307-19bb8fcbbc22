"""微孔板样本谱系追踪 API。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager

from fastapi import Depends, FastAPI, HTTPException, Response

from . import db as db_module
from . import ledger
from .schemas import BatchCreate, ReversalCreate, WellCreate


@asynccontextmanager
async def lifespan(_: FastAPI):
    db_module.init_db()
    yield


app = FastAPI(
    title="微孔板样本谱系追踪 API",
    version="1.1.0",
    description="登记孔位初始样本，原子执行批量移液，追踪各初始样本在每个孔位中的占比；"
    "已提交批次在未被后续操作触及孔位时可凭批前快照原子撤销。",
    lifespan=lifespan,
)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = db_module.connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def immediate_transaction(conn: sqlite3.Connection):
    """进入即取写锁（BEGIN IMMEDIATE）的事务。

    补液登记与撤销都依赖「读取顺序基准 → 写入」不被并发写插队，
    必须在事务开始时就拿到保留锁，避免两个写事务在同一快照下互相漏看。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------- 孔位

@app.post("/wells", status_code=201)
def register_well(payload: WellCreate, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """登记孔位中的初始样本与体积；同一孔位重复登记视为补液混匀。

    补液在同事务写入审计（登记时刻已提交批次的最大 id），供批次撤销判定先后。
    """
    with immediate_transaction(conn):
        ledger.register_initial_sample(conn, payload.well, payload.sample, payload.volume)
        ledger.record_registration(conn, payload.well, payload.sample, payload.volume)
    return ledger.well_state(conn, payload.well)


@app.get("/wells/{well}")
def get_well(well: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """查询孔位当前体积与各初始样本占比。"""
    state = ledger.well_state(conn, well.strip().upper())
    if state is None:
        raise HTTPException(status_code=404, detail=f"孔位 {well} 尚未登记")
    return state


@app.get("/wells/{well}/batches")
def get_well_batches(well: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """查询涉及该孔位（作为来源或目标）的完整批次记录。"""
    well = well.strip().upper()
    if ledger.get_well_volume(conn, well) is None:
        raise HTTPException(status_code=404, detail=f"孔位 {well} 尚未登记")
    rows = conn.execute(
        "SELECT DISTINCT batch_id FROM operations WHERE source = ? OR destination = ? "
        "ORDER BY batch_id",
        (well, well),
    ).fetchall()
    return {
        "well": well,
        "batches": [ledger.batch_payload(conn, row["batch_id"]) for row in rows],
    }


# ---------------------------------------------------------------- 批次

@app.post("/batches")
def submit_batch(
    payload: BatchCreate,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """提交一批移液操作：单事务按序执行，任一步体积不足则整批回滚。

    request_key 幂等：相同键重复提交不会重复执行，直接返回首个结果。
    """
    existing = ledger.find_batch_by_key(conn, payload.request_key)
    if existing is not None:
        response.status_code = 200
        return ledger.batch_payload(conn, existing["id"], replayed=True)

    try:
        with conn:  # 单事务：异常即整体回滚
            batch_id = ledger.execute_batch(conn, payload.request_key, payload.operations)
    except ledger.InsufficientVolume as exc:
        # 状态已回滚；把失败批次也记录下来，消耗掉这个 request_key
        try:
            with conn:
                batch_id = ledger.insert_batch(conn, payload.request_key, "failed", error=str(exc))
        except sqlite3.IntegrityError:
            row = ledger.find_batch_by_key(conn, payload.request_key)
            response.status_code = 200
            return ledger.batch_payload(conn, row["id"], replayed=True)
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "batch_id": batch_id,
                "well": exc.well,
                "available": ledger.dec_str(exc.available),
                "requested": ledger.dec_str(exc.requested),
            },
        )
    except sqlite3.IntegrityError:
        # 并发下同一 request_key 已被写入：按重放处理，不重复执行
        row = ledger.find_batch_by_key(conn, payload.request_key)
        response.status_code = 200
        return ledger.batch_payload(conn, row["id"], replayed=True)

    response.status_code = 201
    return ledger.batch_payload(conn, batch_id)


@app.get("/batches")
def list_batches(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """全部批次记录（含失败批次）。"""
    rows = conn.execute("SELECT id FROM batches ORDER BY id").fetchall()
    return {"batches": [ledger.batch_payload(conn, row["id"]) for row in rows]}


@app.get("/batches/{batch_id}")
def get_batch(batch_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """单个批次的完整记录。"""
    row = conn.execute("SELECT id FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 不存在")
    return ledger.batch_payload(conn, batch_id)


# ---------------------------------------------------------------- 批次撤销

def _reversal_conflict(exc: ledger.ReversalConflict) -> HTTPException:
    detail: dict | str
    if exc.code == "blocked":
        detail = {"code": exc.code, "message": str(exc), **exc.detail}
    else:
        detail = {"code": exc.code, "message": str(exc)}
    return HTTPException(status_code=409, detail=detail)


@app.post("/batches/{batch_id}/reversals", status_code=201)
def reverse_batch(
    batch_id: int,
    payload: ReversalCreate,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """撤销一个已提交批次：按批前快照原子恢复其触及的全部孔位。

    - 只有目标批次已提交、尚未撤销，且之后没有补液或仍生效的已提交批次
      改动过这些孔时才可撤销；条件不符返回 409，列出阻塞孔位与后续操作，
      液体状态保持不变；
    - 上线前提交、没有批前快照的批次明确返回 409（不可撤销）；
    - request_key 幂等：同键同目标返回首次结果（200, replayed=true），
      同键换目标返回 409。原批次保留不删除，另写一条关联撤销审计。
    """
    existing = ledger.find_reversal_by_key(conn, payload.request_key)
    if existing is not None:
        if existing["batch_id"] != batch_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "request_key_target_mismatch",
                    "message": (
                        f"request_key {payload.request_key} 已用于撤销批次 "
                        f"{existing['batch_id']}，不能改用于批次 {batch_id}"
                    ),
                    "reversal_id": existing["id"],
                    "batch_id": existing["batch_id"],
                },
            )
        response.status_code = 200
        return ledger.reversal_payload(conn, existing["id"], existing["batch_id"], replayed=True)

    # 找不到批次在进入写事务前直接 404
    row = conn.execute("SELECT id FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 不存在")

    try:
        # 进入即取写锁：保证冲突检查与恢复在同一最新快照上原子完成
        with immediate_transaction(conn):
            reversal_id = ledger.execute_reversal(conn, batch_id, payload.request_key)
    except ledger.ReversalConflict as exc:
        if exc.code in ("already_replayed", "request_key_target_mismatch"):
            # 并发下写锁内才发现同键撤销已落库：按幂等语义重放/判换目标
            existing = ledger.find_reversal_by_key(conn, payload.request_key)
            if existing is not None and existing["batch_id"] == batch_id:
                response.status_code = 200
                return ledger.reversal_payload(
                    conn, existing["id"], existing["batch_id"], replayed=True
                )
        raise _reversal_conflict(exc)
    except sqlite3.IntegrityError:
        # 并发下同一 request_key 已落库：按重放/换目标处理，不重复撤销
        existing = ledger.find_reversal_by_key(conn, payload.request_key)
        if existing is not None and existing["batch_id"] != batch_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "request_key_target_mismatch",
                    "message": (
                        f"request_key {payload.request_key} 已用于撤销批次 "
                        f"{existing['batch_id']}，不能改用于批次 {batch_id}"
                    ),
                    "reversal_id": existing["id"],
                    "batch_id": existing["batch_id"],
                },
            )
        if existing is None:
            # 非 request_key 冲突（理论上不应发生）：抛出保留原始 500
            raise
        response.status_code = 200
        return ledger.reversal_payload(conn, existing["id"], existing["batch_id"], replayed=True)

    return ledger.reversal_payload(conn, reversal_id, batch_id)
