from __future__ import annotations
from app.db import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS meters (
  meter_id TEXT PRIMARY KEY,
  building TEXT NOT NULL,
  unit_label TEXT NOT NULL,
  meter_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS periods (
  period_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'open',
  sealed_at TEXT,
  batch_no TEXT
);
CREATE TABLE IF NOT EXISTS estimates (
  estimate_id TEXT PRIMARY KEY,
  meter_id TEXT NOT NULL,
  period_id TEXT NOT NULL,
  qty REAL NOT NULL,
  doc_no TEXT NOT NULL,
  loaded_at TEXT NOT NULL,
  receipt_id TEXT,
  UNIQUE(meter_id, period_id, doc_no)
);
CREATE TABLE IF NOT EXISTS actuals (
  actual_id TEXT PRIMARY KEY,
  meter_id TEXT NOT NULL,
  period_id TEXT NOT NULL,
  qty REAL NOT NULL,
  occurred_at TEXT NOT NULL,
  reader_code TEXT NOT NULL,
  doc_no TEXT NOT NULL,
  loaded_at TEXT NOT NULL,
  receipt_id TEXT,
  UNIQUE(meter_id, period_id, doc_no)
);
CREATE TABLE IF NOT EXISTS reconcile_batches (
  batch_id TEXT PRIMARY KEY,
  period_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  matched_count INTEGER NOT NULL DEFAULT 0,
  variance_count INTEGER NOT NULL DEFAULT 0,
  fingerprint TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS variances (
  variance_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL,
  meter_id TEXT NOT NULL,
  period_id TEXT NOT NULL,
  estimate_qty REAL,
  actual_qty REAL,
  delta REAL NOT NULL,
  fate TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS load_receipts (
  receipt_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  period_id TEXT NOT NULL,
  load_mode TEXT NOT NULL,
  status TEXT NOT NULL,
  total_count INTEGER NOT NULL,
  success_count INTEGER NOT NULL,
  fail_count INTEGER NOT NULL,
  reason_codes TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS load_quarantine (
  quarantine_id TEXT PRIMARY KEY,
  receipt_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  period_id TEXT NOT NULL,
  meter_id TEXT,
  doc_no TEXT,
  row_index INTEGER NOT NULL,
  reason_code TEXT NOT NULL,
  reason_text TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quarantine_receipt ON load_quarantine(receipt_id);
CREATE INDEX IF NOT EXISTS idx_quarantine_period ON load_quarantine(period_id);
"""

# 已存在的旧库补建增量对象（CREATE TABLE IF NOT EXISTS 本身幂等，这里保持显式入口）
MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS load_receipts (
  receipt_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  period_id TEXT NOT NULL,
  load_mode TEXT NOT NULL,
  status TEXT NOT NULL,
  total_count INTEGER NOT NULL,
  success_count INTEGER NOT NULL,
  fail_count INTEGER NOT NULL,
  reason_codes TEXT NOT NULL,
  created_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS load_quarantine (
  quarantine_id TEXT PRIMARY KEY,
  receipt_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  period_id TEXT NOT NULL,
  meter_id TEXT,
  doc_no TEXT,
  row_index INTEGER NOT NULL,
  reason_code TEXT NOT NULL,
  reason_text TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_quarantine_receipt ON load_quarantine(receipt_id)",
    "CREATE INDEX IF NOT EXISTS idx_quarantine_period ON load_quarantine(period_id)",
]


def _ensure_receipt_columns(conn) -> None:
    """旧库补溯源列并回填：正式表每行必须能回溯到装载回执（架构一的重放锚点）。

    升级前写入的历史行没有 receipt_id，统一挂到合成的 R-LEGACY-* 回执，
    使「回执 success 累计 = 正式表笔数」在升级后的库上依然成立，不留永久孤儿。
    """
    legacy = {
        "estimates": ("R-LEGACY-EST", "estimate"),
        "actuals": ("R-LEGACY-ACT", "actual"),
    }
    for table, (legacy_rid, kind) in legacy.items():
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "receipt_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN receipt_id TEXT")
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{table}_receipt ON {table}(receipt_id)"
        )
        orphan_n = conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE receipt_id IS NULL"
        ).fetchone()["c"]
        if orphan_n:
            conn.execute(
                f"UPDATE {table} SET receipt_id=? WHERE receipt_id IS NULL", (legacy_rid,)
            )
            exists = conn.execute(
                "SELECT 1 FROM load_receipts WHERE receipt_id=?", (legacy_rid,)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO load_receipts(receipt_id,kind,period_id,load_mode,status,"
                    "total_count,success_count,fail_count,reason_codes,created_at) "
                    "VALUES (?,?, '', 'atomic','success',?,?,0,'{}','1970-01-01T00:00:00')",
                    (legacy_rid, kind, orphan_n, orphan_n),
                )


def seed_if_empty() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            conn.execute(stmt)
        _ensure_receipt_columns(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM meters").fetchone()["c"]
        if n:
            # 既有库：确保架构开关存在，缺省 atomic
            conn.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES ('load_mode','atomic')"
            )
            conn.commit()
            return
        meters = [
            ("M-W-101", "A栋", "101", "water"),
            ("M-W-102", "A栋", "102", "water"),
            ("M-E-101", "A栋", "101", "electric"),
            ("M-W-201", "B栋", "201", "water"),
            ("M-E-201", "B栋", "201", "electric"),
            ("M-E-202", "B栋", "202", "electric"),
        ]
        conn.executemany(
            "INSERT INTO meters(meter_id,building,unit_label,meter_type,status) VALUES (?,?,?,?, 'active')",
            meters,
        )
        conn.execute(
            "INSERT INTO periods(period_id,status) VALUES ('2026-10','open')"
        )
        conn.execute(
            "INSERT INTO periods(period_id,status) VALUES ('2026-09','open')"
        )
        # estimates for 2026-10
        est = [
            ("E1", "M-W-101", "2026-10", 12.0, "EST-101", "2026-10-01T08:00:00"),
            ("E2", "M-W-102", "2026-10", 15.0, "EST-102", "2026-10-01T08:00:00"),
            ("E3", "M-E-101", "2026-10", 220.0, "EST-E101", "2026-10-01T08:00:00"),
            ("E4", "M-W-201", "2026-10", 18.0, "EST-201", "2026-10-01T08:00:00"),
            ("E5", "M-E-201", "2026-10", 310.0, "EST-E201", "2026-10-01T08:00:00"),
            ("E6", "M-E-202", "2026-10", 280.0, "EST-E202", "2026-10-01T08:00:00"),
        ]
        conn.executemany(
            "INSERT INTO estimates(estimate_id,meter_id,period_id,qty,doc_no,loaded_at,receipt_id) "
            "VALUES (?,?,?,?,?,?, 'R-SEED-EST')",
            est,
        )
        # actuals: most match; two variance
        act = [
            ("A1", "M-W-101", "2026-10", 12.0, "2026-10-28T10:00:00", "R01", "ACT-101", "2026-10-28T10:05:00"),
            ("A2", "M-W-102", "2026-10", 14.0, "2026-10-28T10:10:00", "R01", "ACT-102", "2026-10-28T10:15:00"),  # short
            ("A3", "M-E-101", "2026-10", 220.0, "2026-10-28T11:00:00", "R02", "ACT-E101", "2026-10-28T11:05:00"),
            ("A4", "M-W-201", "2026-10", 18.0, "2026-10-28T11:20:00", "R02", "ACT-201", "2026-10-28T11:25:00"),
            ("A5", "M-E-201", "2026-10", 330.0, "2026-10-28T12:00:00", "R03", "ACT-E201", "2026-10-28T12:05:00"),  # over
            ("A6", "M-E-202", "2026-10", 280.0, "2026-10-28T12:20:00", "R03", "ACT-E202", "2026-10-28T12:25:00"),
        ]
        conn.executemany(
            "INSERT INTO actuals(actual_id,meter_id,period_id,qty,occurred_at,reader_code,doc_no,loaded_at,receipt_id) "
            "VALUES (?,?,?,?,?,?,?,?, 'R-SEED-ACT')",
            act,
        )
        conn.execute(
            "INSERT INTO settings(key,value) VALUES ('current_period','2026-10')"
        )
        conn.execute(
            "INSERT INTO settings(key,value) VALUES ('product_title','抄表簿')"
        )
        # 全局唯一装载架构：atomic=整批原子回滚；split=成功行落库/失败行进隔离
        conn.execute(
            "INSERT INTO settings(key,value) VALUES ('load_mode','atomic')"
        )
        # 种子正式行同样挂回执：保证「回执 success 累计 = 正式表笔数」从初始库即成立
        conn.execute(
            "INSERT INTO load_receipts(receipt_id,kind,period_id,load_mode,status,"
            "total_count,success_count,fail_count,reason_codes,created_at) "
            "VALUES ('R-SEED-EST','estimate','2026-10','atomic','success',6,6,0,'{}','2026-10-01T08:00:00')"
        )
        conn.execute(
            "INSERT INTO load_receipts(receipt_id,kind,period_id,load_mode,status,"
            "total_count,success_count,fail_count,reason_codes,created_at) "
            "VALUES ('R-SEED-ACT','actual','2026-10','atomic','success',6,6,0,'{}','2026-10-28T10:05:00')"
        )
        # 界面不露英文：表类型仍存 water/electric 码，前端映射为中文
        conn.commit()
    finally:
        conn.close()
