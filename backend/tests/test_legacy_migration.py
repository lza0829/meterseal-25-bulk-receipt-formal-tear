"""旧库迁移：历史正式行无 receipt_id，升级回填合成回执后五链仍对齐。"""
import os
import sqlite3

import app.db as db_mod
from app import seed
from conftest import count


def test_legacy_db_backfills_receipt_anchors_and_replays(client):
    # 造一个"升级前"旧库：正式表没有 receipt_id 列，也没有回执表
    legacy_db = os.path.join(os.path.dirname(db_mod.DB_PATH), "legacy.db")
    if os.path.exists(legacy_db):
        os.remove(legacy_db)
    old = sqlite3.connect(legacy_db)
    old.executescript(
        """
        CREATE TABLE meters(meter_id TEXT PRIMARY KEY, building TEXT NOT NULL,
            unit_label TEXT NOT NULL, meter_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active');
        CREATE TABLE periods(period_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'open',
            sealed_at TEXT, batch_no TEXT);
        CREATE TABLE estimates(estimate_id TEXT PRIMARY KEY, meter_id TEXT NOT NULL,
            period_id TEXT NOT NULL, qty REAL NOT NULL, doc_no TEXT NOT NULL,
            loaded_at TEXT NOT NULL, UNIQUE(meter_id,period_id,doc_no));
        CREATE TABLE actuals(actual_id TEXT PRIMARY KEY, meter_id TEXT NOT NULL,
            period_id TEXT NOT NULL, qty REAL NOT NULL, occurred_at TEXT NOT NULL,
            reader_code TEXT NOT NULL, doc_no TEXT NOT NULL, loaded_at TEXT NOT NULL,
            UNIQUE(meter_id,period_id,doc_no));
        CREATE TABLE reconcile_batches(batch_id TEXT PRIMARY KEY, period_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open', matched_count INTEGER NOT NULL DEFAULT 0,
            variance_count INTEGER NOT NULL DEFAULT 0, fingerprint TEXT, created_at TEXT NOT NULL);
        CREATE TABLE variances(variance_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
            meter_id TEXT NOT NULL, period_id TEXT NOT NULL, estimate_qty REAL,
            actual_qty REAL, delta REAL NOT NULL, fate TEXT NOT NULL DEFAULT 'pending');
        CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    old.execute("INSERT INTO meters VALUES ('M1','A','1','water','active')")
    old.execute("INSERT INTO periods(period_id,status) VALUES ('2026-08','open')")
    old.execute("INSERT INTO estimates VALUES ('LE1','M1','2026-08',5.0,'OLD-E','t')")
    old.execute("INSERT INTO actuals VALUES ('LA1','M1','2026-08',5.0,'t','R','OLD-A','t')")
    old.commit()
    old.close()

    saved = db_mod.DB_PATH
    db_mod.DB_PATH = legacy_db
    try:
        seed.seed_if_empty()  # 触发迁移回填
        conn = sqlite3.connect(legacy_db)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(estimates)")}
            assert "receipt_id" in cols
            assert conn.execute(
                "SELECT COUNT(*) FROM estimates WHERE receipt_id='R-LEGACY-EST'"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM actuals WHERE receipt_id='R-LEGACY-ACT'"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT success_count FROM load_receipts WHERE receipt_id='R-LEGACY-EST'"
            ).fetchone()[0] == 1
            # 无残留孤儿
            assert conn.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT receipt_id FROM estimates WHERE receipt_id IS NULL "
                "UNION ALL SELECT receipt_id FROM actuals WHERE receipt_id IS NULL)"
            ).fetchone()[0] == 0
        finally:
            conn.close()

        # 通过应用层五链重放：升级库一进来就是对齐的
        from fastapi.testclient import TestClient
        from app.main import app
        r = TestClient(app).get("/api/health").json()
        assert r["five_chain_aligned"] is True, r["five_chain"]["breaches"]
    finally:
        db_mod.DB_PATH = saved
        os.remove(legacy_db)
