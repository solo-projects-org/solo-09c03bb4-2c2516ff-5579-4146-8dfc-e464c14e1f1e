"""SQLite 持久化层。

设计要点
========
* 所有写操作使用 ``BEGIN IMMEDIATE`` 立即获取写锁，保证“同一完整身份状态”与
  “失败不留半批”（整事务提交或回滚）。
* 统一号由 ``id_seq`` 单调分配，仅在真正创建新组时消耗，已分配号码永不复用。
* 组是“代”的概念：``current`` 为 1 的组构成当前身份状态，合并/拆分时旧组置
  ``current=0`` 并为每个受影响的新组分配全新统一号。
* 观测同时保存“接纳时统一号”（snapshot_unified_id）与“来源站身份”，查询时再
  解析当前归属，因此历史观测可随沿革动态呈现当前归属。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

DB_PATH = os.environ.get("STATION_DB", "/data/stations.db")

STATION_CAP = 100  # 总库至多 100 站（来源站总数与当前组数均受此约束）

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS id_seq (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_unified_id INTEGER NOT NULL
);
INSERT OR IGNORE INTO id_seq(id, next_unified_id) VALUES (1, 1);

CREATE TABLE IF NOT EXISTS app_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
INSERT OR IGNORE INTO app_state(id, version) VALUES (1, 1);

-- 来源站：(来源, 源站号) 为业务主键，全局至多 100 个
CREATE TABLE IF NOT EXISTS source_station (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_station_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_ver INTEGER NOT NULL,
    UNIQUE(source, source_station_id)
);

-- 统一站组（统一号 = 代身份）
CREATE TABLE IF NOT EXISTS station_group (
    unified_id INTEGER PRIMARY KEY,
    created_ver INTEGER NOT NULL,
    retired_ver INTEGER,          -- NULL 表示当前组
    current INTEGER NOT NULL,     -- 1 当前 / 0 已退役
    created_op INTEGER REFERENCES op_log(op_id)
);

-- 组成员关系（只增不改：组退役后行保留，用于沿革追溯）
CREATE TABLE IF NOT EXISTS group_member (
    unified_id INTEGER NOT NULL REFERENCES station_group(unified_id),
    source_station_pk INTEGER NOT NULL REFERENCES source_station(id),
    PRIMARY KEY (unified_id, source_station_pk)
);

CREATE TABLE IF NOT EXISTS op_log (
    op_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,           -- upload | merge | split
    base_ver INTEGER,             -- 页面携带的身份状态版本（upload 可空）
    from_ver INTEGER NOT NULL,
    to_ver INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL      -- UTC ISO8601
);

CREATE TABLE IF NOT EXISTS observation (
    obs_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    source_station_id TEXT NOT NULL,
    obs_time TEXT NOT NULL,       -- 规范化后的 UTC 时刻
    temperature REAL NOT NULL,
    snapshot_unified_id INTEGER NOT NULL,  -- 接纳时的当前统一号（历史保留）
    ingest_ver INTEGER NOT NULL,  -- 入仓时身份状态版本
    created_at TEXT NOT NULL,
    UNIQUE(source, source_station_id, obs_time)
);
CREATE INDEX IF NOT EXISTS idx_obs_batch ON observation(batch_id);
CREATE INDEX IF NOT EXISTS idx_obs_src ON observation(source, source_station_id);
CREATE INDEX IF NOT EXISTS idx_obs_time ON observation(obs_time);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """打开一个连接：外键开启、WAL、行忙时等待而非立即报错。"""
    path = db_path or DB_PATH
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: str | None = None) -> None:
    """初始化/迁移数据库（幂等）。"""
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即写事务：进入即抢写锁，正常退出提交，异常回滚（不留半批）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------- 基础读写辅助 ----------

def get_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT version FROM app_state WHERE id=1").fetchone()
    return int(row["version"])


def bump_version(conn: sqlite3.Connection) -> int:
    conn.execute("UPDATE app_state SET version = version + 1 WHERE id=1")
    return get_version(conn)


def alloc_unified_ids(conn: sqlite3.Connection, n: int) -> list[int]:
    """消耗 n 个全新统一号；号码单调递增、永不复用。"""
    row = conn.execute("SELECT next_unified_id FROM id_seq WHERE id=1").fetchone()
    first = int(row["next_unified_id"])
    conn.execute(
        "UPDATE id_seq SET next_unified_id = ? WHERE id=1", (first + n,)
    )
    return list(range(first, first + n))


def create_group(conn: sqlite3.Connection, unified_id: int, ver: int,
                 members: list[int], op_id: int | None) -> None:
    conn.execute(
        "INSERT INTO station_group(unified_id, created_ver, retired_ver, "
        "current, created_op) VALUES (?, ?, NULL, 1, ?)",
        (unified_id, ver, op_id),
    )
    conn.executemany(
        "INSERT INTO group_member(unified_id, source_station_pk) VALUES (?, ?)",
        [(unified_id, pk) for pk in members],
    )


def retire_group(conn: sqlite3.Connection, unified_id: int, ver: int) -> None:
    conn.execute(
        "UPDATE station_group SET current=0, retired_ver=? WHERE unified_id=?",
        (ver, unified_id),
    )


def current_membership(conn: sqlite3.Connection) -> dict[int, int]:
    """当前归属：source_station 主键 -> 当前统一号。"""
    rows = conn.execute(
        "SELECT gm.source_station_pk, gm.unified_id "
        "FROM group_member gm JOIN station_group g ON g.unified_id = gm.unified_id "
        "WHERE g.current = 1"
    ).fetchall()
    return {int(r["source_station_pk"]): int(r["unified_id"]) for r in rows}


def get_current_group(conn: sqlite3.Connection, unified_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM station_group WHERE unified_id=? AND current=1",
        (unified_id,),
    ).fetchone()


def group_member_pks(conn: sqlite3.Connection, unified_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT source_station_pk FROM group_member WHERE unified_id=? "
        "ORDER BY source_station_pk",
        (unified_id,),
    ).fetchall()
    return [int(r["source_station_pk"]) for r in rows]


def count_source_stations(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) AS c FROM source_station"
    ).fetchone()["c"])


def count_current_groups(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) AS c FROM station_group WHERE current=1"
    ).fetchone()["c"])
