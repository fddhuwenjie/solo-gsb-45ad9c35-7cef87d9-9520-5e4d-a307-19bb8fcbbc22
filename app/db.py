"""SQLite 连接与建表。

所有体积/组分量都以 TEXT 存储 Decimal 的字符串形式，
避免 SQLite NUMERIC 亲和性把值转成 REAL 丢失精度。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "lineage.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS wells (
    well    TEXT PRIMARY KEY,
    volume  TEXT NOT NULL DEFAULT '0'
);

CREATE TABLE IF NOT EXISTS samples (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS well_composition (
    well        TEXT NOT NULL REFERENCES wells(well),
    sample_id   INTEGER NOT NULL REFERENCES samples(id),
    amount      TEXT NOT NULL,
    PRIMARY KEY (well, sample_id)
);

CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL CHECK (status IN ('committed', 'failed')),
    error       TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    step        INTEGER NOT NULL,
    source      TEXT NOT NULL,
    destination TEXT NOT NULL,
    volume      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_operations_batch ON operations(batch_id);
CREATE INDEX IF NOT EXISTS idx_operations_well ON operations(source, destination);

-- 批前快照：批次首次触及某孔（作为来源或目标）之前该孔的完整状态。
-- existed=0 表示该孔在批前尚未登记（撤销时须连孔一起删除）。
CREATE TABLE IF NOT EXISTS batch_well_snapshots (
    batch_id  INTEGER NOT NULL REFERENCES batches(id),
    well      TEXT NOT NULL,
    existed   INTEGER NOT NULL,
    volume    TEXT NOT NULL,
    PRIMARY KEY (batch_id, well)
);

CREATE TABLE IF NOT EXISTS snapshot_composition (
    batch_id  INTEGER NOT NULL REFERENCES batches(id),
    well      TEXT NOT NULL,
    sample_id INTEGER NOT NULL REFERENCES samples(id),
    amount    TEXT NOT NULL,
    PRIMARY KEY (batch_id, well, sample_id)
);

-- 撤销审计记录：不删除原批次，仅在此登记一次原子撤销。
CREATE TABLE IF NOT EXISTS reversals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_key TEXT NOT NULL UNIQUE,
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reversals_batch ON reversals(batch_id);

-- 补液审计：记录每次 POST /wells 发生时已提交批次的最大 id，
-- 供撤销时判断「该孔之后是否有补液」。上线前的补液不在表中——
-- 但上线前批次同样没有快照，本来就不可撤销。
CREATE TABLE IF NOT EXISTS registrations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    well            TEXT NOT NULL,
    sample          TEXT NOT NULL,
    volume          TEXT NOT NULL,
    after_batch_id  INTEGER,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_registrations_well_batch
    ON registrations(well, after_batch_id);
"""


def db_path() -> Path:
    """数据库路径，可用环境变量 LINEAGE_DB 覆盖（测试用）。"""
    return Path(os.environ.get("LINEAGE_DB", str(DEFAULT_DB_PATH)))


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
