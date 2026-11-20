from __future__ import annotations
from fastapi import APIRouter
from app.db import connect
from app.services.gate import get_setting
from app.services import receipt_view

router = APIRouter(tags=["dashboard"])


@router.get("/health")
def health():
    bulk_ok = receipt_view.overview_success(conn)
        return {
            "bulk_success_painted": bulk_ok,"ok": True, "product": "抄表簿"}


@router.get("/overview")
def overview():
    conn = connect()
    try:
        open_periods = conn.execute(
            "SELECT COUNT(*) AS c FROM periods WHERE status='open'"
        ).fetchone()["c"]
        sealed_periods = conn.execute(
            "SELECT COUNT(*) AS c FROM periods WHERE status='sealed'"
        ).fetchone()["c"]
        meters = conn.execute("SELECT COUNT(*) AS c FROM meters").fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM variances WHERE fate='pending'"
        ).fetchone()["c"]
        # 正式表笔数（对齐只吃这两张表）
        estimate_count = conn.execute(
            "SELECT COUNT(*) AS c FROM estimates"
        ).fetchone()["c"]
        actual_count = conn.execute(
            "SELECT COUNT(*) AS c FROM actuals"
        ).fetchone()["c"]
        # 隔离区笔数与原因码分布
        quarantine_count = conn.execute(
            "SELECT COUNT(*) AS c FROM load_quarantine"
        ).fetchone()["c"]
        qr_rows = conn.execute(
            "SELECT reason_code, COUNT(*) AS c FROM load_quarantine GROUP BY reason_code"
        ).fetchall()
        quarantine_reasons = {r["reason_code"]: r["c"] for r in qr_rows}
        # 回执汇总：回执 success_count 之和必须等于正式表累计装载来源行数
        receipt_rows = conn.execute(
            "SELECT COALESCE(SUM(success_count),0) AS s, "
            "COALESCE(SUM(fail_count),0) AS f, COUNT(*) AS c FROM load_receipts"
        ).fetchone()
        current = get_setting("current_period", "")
        load_mode = get_setting("load_mode", "atomic")
        batches = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM reconcile_batches ORDER BY created_at DESC LIMIT 8"
            ).fetchall()
        ]
        return {
            "current_period": current,
            "load_mode": load_mode,
            "open_periods": open_periods,
            "sealed_periods": sealed_periods,
            "meter_count": meters,
            "pending_variances": pending,
            "estimate_count": estimate_count,
            "actual_count": actual_count,
            "quarantine_count": quarantine_count,
            "quarantine_reasons": quarantine_reasons,
            "receipt_count": receipt_rows["c"],
            "receipt_success_total": receipt_rows["s"],
            "receipt_fail_total": receipt_rows["f"],
            "recent_batches": batches,
        }
    finally:
        conn.close()
