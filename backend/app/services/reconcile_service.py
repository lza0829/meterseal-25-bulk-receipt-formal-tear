from __future__ import annotations
import hashlib
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run_align(period_id: str) -> dict:
    conn = connect()
    try:
        # 拿写锁后在事务内复查账期状态，与装载/封印事务互斥，消除 TOCTOU
        conn.execute("BEGIN IMMEDIATE")
        period = conn.execute(
            "SELECT status FROM periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if not period:
            conn.rollback()
            raise HTTPException(404, f"账期不存在: {period_id}")
        if period["status"] == "sealed":
            conn.rollback()
            raise HTTPException(409, f"账期已封印，禁止对齐: {period_id}")

        # 对齐只吃正式表 estimates/actuals；load_quarantine 的行从未进正式表，
        # 因此隔离行不可能出现在 matched / variances 中（split 架构的硬边界）。
        est = {
            r["meter_id"]: r
            for r in conn.execute(
                "SELECT * FROM estimates WHERE period_id=?", (period_id,)
            ).fetchall()
        }
        act = {
            r["meter_id"]: r
            for r in conn.execute(
                "SELECT * FROM actuals WHERE period_id=?", (period_id,)
            ).fetchall()
        }
        keys = sorted(set(est) | set(act))
        if not keys:
            conn.rollback()
            raise HTTPException(400, "该账期无估抄/实抄可对齐")

        # expire prior open batches for period
        conn.execute(
            "UPDATE reconcile_batches SET status='expired' WHERE period_id=? AND status='open'",
            (period_id,),
        )

        batch_id = "B-" + uuid.uuid4().hex[:10]
        matched = 0
        variances = []
        for mid in keys:
            eq = est[mid]["qty"] if mid in est else None
            aq = act[mid]["qty"] if mid in act else None
            if eq is not None and aq is not None and abs(eq - aq) < 1e-9:
                matched += 1
                continue
            delta = (aq if aq is not None else 0.0) - (eq if eq is not None else 0.0)
            variances.append(
                (
                    "V-" + uuid.uuid4().hex[:10],
                    batch_id,
                    mid,
                    period_id,
                    eq,
                    aq,
                    delta,
                    "pending",
                )
            )

        fp_src = f"{period_id}|{matched}|{len(variances)}|" + ",".join(keys)
        fingerprint = hashlib.sha256(fp_src.encode()).hexdigest()[:16]
        conn.execute(
            """INSERT INTO reconcile_batches(batch_id,period_id,status,matched_count,variance_count,fingerprint,created_at)
               VALUES (?,?, 'open',?,?,?,?)""",
            (batch_id, period_id, matched, len(variances), fingerprint, _now()),
        )
        if variances:
            conn.executemany(
                """INSERT INTO variances(variance_id,batch_id,meter_id,period_id,estimate_qty,actual_qty,delta,fate)
                   VALUES (?,?,?,?,?,?,?,?)""",
                variances,
            )
        conn.commit()
        return {
            "batch_id": batch_id,
            "period_id": period_id,
            "matched_count": matched,
            "variance_count": len(variances),
            "fingerprint": fingerprint,
            "status": "open",
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"对齐事务异常已回滚: {e}") from e
    finally:
        conn.close()


def seal_period(period_id: str, batch_id: str | None = None) -> dict:
    conn = connect()
    try:
        # 拿写锁后在事务内复查账期状态，与装载事务互斥
        conn.execute("BEGIN IMMEDIATE")
        period = conn.execute(
            "SELECT * FROM periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if not period:
            conn.rollback()
            raise HTTPException(404, "账期不存在")
        if period["status"] == "sealed":
            conn.rollback()
            raise HTTPException(409, "账期已封印")

        if batch_id:
            batch = conn.execute(
                "SELECT * FROM reconcile_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if not batch or batch["period_id"] != period_id:
                conn.rollback()
                raise HTTPException(400, "对账批与账期不匹配")
            if batch["status"] not in ("open",):
                conn.rollback()
                raise HTTPException(409, "对账批不可用")
        else:
            batch = conn.execute(
                """SELECT * FROM reconcile_batches WHERE period_id=? AND status='open'
                   ORDER BY created_at DESC LIMIT 1""",
                (period_id,),
            ).fetchone()
            if not batch:
                conn.rollback()
                raise HTTPException(400, "无 open 对账批，请先对齐")
            batch_id = batch["batch_id"]

        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM variances WHERE batch_id=? AND fate='pending'",
            (batch_id,),
        ).fetchone()["c"]
        # base: allow seal with pending (later topics can harden)
        sealed_at = _now()
        conn.execute(
            "UPDATE periods SET status='sealed', sealed_at=?, batch_no=? WHERE period_id=?",
            (sealed_at, batch_id, period_id),
        )
        conn.execute(
            "UPDATE reconcile_batches SET status='sealed' WHERE batch_id=?",
            (batch_id,),
        )
        conn.commit()
        return {
            "period_id": period_id,
            "batch_id": batch_id,
            "sealed_at": sealed_at,
            "pending_left": pending,
            "status": "sealed",
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"封印事务异常已回滚: {e}") from e
    finally:
        conn.close()


def set_fate(variance_id: str, fate: str) -> dict:
    if fate not in ("pending", "accept_short", "accept_over", "quarantine"):
        raise HTTPException(400, "非法命运")
    conn = connect()
    try:
        row = conn.execute(
            "SELECT v.*, p.status AS period_status FROM variances v JOIN periods p ON p.period_id=v.period_id WHERE v.variance_id=?",
            (variance_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "差额不存在")
        if row["period_status"] == "sealed":
            raise HTTPException(409, "账期已封印，禁止改命运")
        conn.execute("UPDATE variances SET fate=? WHERE variance_id=?", (fate, variance_id))
        conn.commit()
        return {"variance_id": variance_id, "fate": fate}
    finally:
        conn.close()
