"""微孔板样本谱系追踪 API。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager

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
    version="1.0.0",
    description="登记孔位初始样本，原子执行批量移液，追踪各初始样本在每个孔位中的占比。",
    lifespan=lifespan,
)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = db_module.connect()
    try:
        yield conn
    finally:
        conn.close()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------- 孔位

@app.post("/wells", status_code=201)
def register_well(payload: WellCreate, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """登记孔位中的初始样本与体积；同一孔位重复登记视为补液混匀。"""
    with conn:
        ledger.register_initial_sample(conn, payload.well, payload.sample, payload.volume)
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


@app.post("/batches/{batch_id}/reversals")
def reverse_batch(
    batch_id: int,
    payload: ReversalCreate,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """撤销一个已提交批次：原子恢复其批前快照并追加审计记录，不删除原批次。

    仅当目标批次已提交、尚未撤销、保存了批前快照，且之后没有补液或已提交
    批次触及相关孔位时可撤销；条件不符返回 409 并列出阻塞孔位与后续操作，
    液体状态保持不变。request_key 幂等：同键同目标返回首次结果（200），
    同键换目标返回 409。
    """
    existing = ledger.find_reversal_by_key(conn, payload.request_key)
    if existing is not None:
        if int(existing["batch_id"]) != batch_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        f"request_key {payload.request_key} 已用于撤销批次 "
                        f"{existing['batch_id']}，不能再用于批次 {batch_id}"
                    ),
                    "reason": "request_key_conflict",
                    "batch_id": batch_id,
                    "existing_batch_id": int(existing["batch_id"]),
                },
            )
        response.status_code = 200
        return ledger.get_reversal(conn, int(existing["id"]), replayed=True)

    try:
        with conn:  # 校验、快照恢复与审计记录同一事务：冲突或异常即整体回滚
            reversal_id = ledger.apply_reversal(conn, batch_id, payload.request_key)
    except ledger.ReversalConflict as exc:
        if exc.reason == "not_found":
            raise HTTPException(status_code=404, detail=exc.message)
        detail: dict = {"message": exc.message, "reason": exc.reason, "batch_id": batch_id}
        if exc.blockers:
            detail["blocking_wells"] = exc.blockers
        raise HTTPException(status_code=409, detail=detail)
    except sqlite3.IntegrityError:
        # 并发下 request_key 或批次撤销状态已被另一请求写入
        row = ledger.find_reversal_by_key(conn, payload.request_key)
        if row is not None:
            if int(row["batch_id"]) != batch_id:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": (
                            f"request_key {payload.request_key} 已用于撤销批次 "
                            f"{row['batch_id']}，不能再用于批次 {batch_id}"
                        ),
                        "reason": "request_key_conflict",
                        "batch_id": batch_id,
                        "existing_batch_id": int(row["batch_id"]),
                    },
                )
            response.status_code = 200
            return ledger.get_reversal(conn, int(row["id"]), replayed=True)
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"批次 {batch_id} 已经撤销，不能重复撤销",
                "reason": "already_reversed",
                "batch_id": batch_id,
            },
        )

    response.status_code = 201
    return ledger.get_reversal(conn, reversal_id)
