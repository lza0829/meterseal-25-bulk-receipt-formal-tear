from __future__ import annotations
from fastapi import APIRouter
from app.db import connect

router = APIRouter(tags=["dashboard"])


def _five_chain_replay(conn) -> dict:
    """架构一总对账：以正式∪隔离行集合重放全部回执，五条链必须同时成立。

    1. 回执笔数：每条回执 total = success + fail；
    2. 正式链：正式表总数 = 回执 success 累计，且每批 receipt_id 回查笔数等于其 success_count；
    3. 隔离链：隔离区总数 = split 回执 fail 累计（atomic/rejected 的失败不进隔离），
       且每批回查笔数与其口径一致；
    4. 键链：正式行无空 receipt_id（每行都可由回执解释，对齐只吃这种可解释正式行）；
    5. 失败链：failed/rejected 回执在正式表零行。
    """
    receipts = conn.execute("SELECT * FROM load_receipts").fetchall()
    est_by_receipt = {
        r["receipt_id"]: r["c"]
        for r in conn.execute(
            "SELECT receipt_id, COUNT(*) AS c FROM estimates GROUP BY receipt_id"
        ).fetchall()
    }
    act_by_receipt = {
        r["receipt_id"]: r["c"]
        for r in conn.execute(
            "SELECT receipt_id, COUNT(*) AS c FROM actuals GROUP BY receipt_id"
        ).fetchall()
    }
    q_by_receipt = {
        r["receipt_id"]: r["c"]
        for r in conn.execute(
            "SELECT receipt_id, COUNT(*) AS c FROM load_quarantine GROUP BY receipt_id"
        ).fetchall()
    }

    formal_total = sum(est_by_receipt.values()) + sum(act_by_receipt.values())
    quarantine_total = sum(q_by_receipt.values())
    receipt_success_sum = 0
    receipt_split_fail_sum = 0
    aligned = True
    breaches: list[str] = []

    for rcpt in receipts:
        rid = rcpt["receipt_id"]
        if rcpt["total_count"] != rcpt["success_count"] + rcpt["fail_count"]:
            aligned = False
            breaches.append(f"{rid}: 笔数不闭合")
        receipt_success_sum += rcpt["success_count"]
        if rcpt["load_mode"] == "split":
            receipt_split_fail_sum += rcpt["fail_count"]

        formal_for_receipt = est_by_receipt.get(rid, 0) + act_by_receipt.get(rid, 0)
        if formal_for_receipt != rcpt["success_count"]:
            aligned = False
            breaches.append(
                f"{rid}: 回执成功 {rcpt['success_count']} vs 正式落库 {formal_for_receipt}"
            )
        if rcpt["status"] in ("failed", "rejected") and formal_for_receipt != 0:
            aligned = False
            breaches.append(f"{rid}: 失败/拒收回执在正式表留有 {formal_for_receipt} 行")

        expected_q = rcpt["fail_count"] if rcpt["load_mode"] == "split" else 0
        if q_by_receipt.get(rid, 0) != expected_q:
            aligned = False
            breaches.append(
                f"{rid}: 隔离应落 {expected_q} vs 实际 {q_by_receipt.get(rid, 0)}"
            )

    if formal_total != receipt_success_sum:
        aligned = False
        breaches.append(f"正式表 {formal_total} != 回执成功累计 {receipt_success_sum}")
    if quarantine_total != receipt_split_fail_sum:
        aligned = False
        breaches.append(
            f"隔离区 {quarantine_total} != split 回执失败累计 {receipt_split_fail_sum}"
        )
    orphan_formal = conn.execute(
        "SELECT COUNT(*) AS c FROM ("
        "SELECT receipt_id FROM estimates WHERE receipt_id IS NULL "
        "UNION ALL SELECT receipt_id FROM actuals WHERE receipt_id IS NULL)"
    ).fetchone()["c"]
    if orphan_formal:
        aligned = False
        breaches.append(f"{orphan_formal} 行正式读数无回执溯源，对齐不得采信")

    return {
        "aligned": aligned,
        "formal_total": formal_total,
        "quarantine_total": quarantine_total,
        "receipt_success_total": receipt_success_sum,
        "receipt_split_fail_total": receipt_split_fail_sum,
        "breaches": breaches,
    }


@router.get("/health")
def health():
    conn = connect()
    try:
        replay = _five_chain_replay(conn)
        return {
            "ok": True,
            "product": "抄表簿",
            "five_chain_aligned": replay["aligned"],
            "five_chain": {k: v for k, v in replay.items() if k != "aligned"},
        }
    finally:
        conn.close()


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
        # 正式表笔数（对齐只吃这两张表）——总览装载计数跟库，不跟回执粉刷值
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
        # 回执汇总：架构一下 success 累计恒等于正式表装载来源行数
        receipt_rows = conn.execute(
            "SELECT COALESCE(SUM(success_count),0) AS s, "
            "COALESCE(SUM(fail_count),0) AS f, COUNT(*) AS c FROM load_receipts"
        ).fetchone()
        current = conn.execute(
            "SELECT value FROM settings WHERE key='current_period'"
        ).fetchone()
        current_period = current["value"] if current else ""
        mode_row = conn.execute(
            "SELECT value FROM settings WHERE key='load_mode'"
        ).fetchone()
        load_mode = mode_row["value"] if mode_row and mode_row["value"] in ("atomic", "split") else "atomic"
        batches = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM reconcile_batches ORDER BY created_at DESC LIMIT 8"
            ).fetchall()
        ]
        replay = _five_chain_replay(conn)
        return {
            "current_period": current_period,
            "load_mode": load_mode,
            "open_periods": open_periods,
            "sealed_periods": sealed_periods,
            "meter_count": meters,
            "pending_variances": pending,
            "estimate_count": estimate_count,
            "actual_count": actual_count,
            "formal_total": estimate_count + actual_count,
            "quarantine_count": quarantine_count,
            "quarantine_reasons": quarantine_reasons,
            "receipt_count": receipt_rows["c"],
            "receipt_success_total": receipt_rows["s"],
            "receipt_fail_total": receipt_rows["f"],
            "five_chain_aligned": replay["aligned"],
            "five_chain_breaches": replay["breaches"],
            "recent_batches": batches,
        }
    finally:
        conn.close()
