from __future__ import annotations
import sqlite3
from app.config import DATA_DIR, DB_PATH


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # 并发写时等待锁而不是立刻报 SQLITE_BUSY，配合 BEGIN IMMEDIATE 串行化写事务
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
