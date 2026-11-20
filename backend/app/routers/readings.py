from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.services import receipt_view

from app.db import connect
from app.services.loader import load_batch

router = APIRouter(tags=["readings"])


class EstimateIn(BaseModel):
    meter_id: str
    period_id: str
    qty: float
    doc_no: str = Field(min_length=1)


class ActualIn(BaseModel):
    meter_id: str
    period_id: str
    qty: float
    occurred_at: str
    reader_code: str = "R00"
    doc_no: str = Field(min_length=1)


class EstimateBatchIn(BaseModel):
    period_id: str | None = None
    rows: list[dict] = Field(min_length=1)


class ActualBatchIn(BaseModel):
    period_id: str | None = None
    rows: list[dict] = Field(min_length=1)


@router.get("/estimates")
def list_estimates(period_id: str | None = None):
    conn = connect()
    try:
        if period_id:
            rows = conn.execute(
                "SELECT * FROM estimates WHERE period_id=? ORDER BY meter_id", (period_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM estimates ORDER BY period_id, meter_id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/estimates")
def create_estimate(body: EstimateIn):
    # 单行也走统一装载服务，保证校验器/封印闸/回执口径只有一套
    receipt = load_batch("estimate", [body.model_dump()], body.period_id)
    if receipt["status"] == "success":
        return {"estimate_id": receipt["loaded_ids"][0], "ok": True,
                "receipt_id": receipt["receipt_id"]}
    f = receipt["failures"][0]
    code = f["reason_code"]
    status = 409 if code == "PERIOD_SEALED" else (404 if code in ("METER_NOT_FOUND", "PERIOD_NOT_FOUND") else 400)
    raise HTTPException(status, f"{f['reason_text']}（receipt={receipt['receipt_id']}）")


@router.post("/estimates/batch")
def create_estimate_batch(body: EstimateBatchIn):
    return load_batch("estimate", body.rows, body.period_id)


@router.get("/actuals")
def list_actuals(period_id: str | None = None):
    conn = connect()
    try:
        if period_id:
            rows = conn.execute(
                "SELECT * FROM actuals WHERE period_id=? ORDER BY meter_id", (period_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM actuals ORDER BY period_id, meter_id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/actuals")
def create_actual(body: ActualIn):
    receipt = load_batch("actual", [body.model_dump()], body.period_id)
    if receipt["status"] == "success":
        return {"actual_id": receipt["loaded_ids"][0], "ok": True,
                "receipt_id": receipt["receipt_id"]}
    f = receipt["failures"][0]
    code = f["reason_code"]
    status = 409 if code == "PERIOD_SEALED" else (404 if code in ("METER_NOT_FOUND", "PERIOD_NOT_FOUND") else 400)
    raise HTTPException(status, f"{f['reason_text']}（receipt={receipt['receipt_id']}）")


@router.post("/actuals/batch")
def create_actual_batch(body: ActualBatchIn):
    return load_batch("actual", body.rows, body.period_id)


@router.get("/quarantine")
def list_quarantine(period_id: str | None = None, receipt_id: str | None = None):
    conn = connect()
    try:
        sql = "SELECT * FROM load_quarantine WHERE 1=1"
        params: list = []
        if period_id:
            sql += " AND period_id=?"
            params.append(period_id)
        if receipt_id:
            sql += " AND receipt_id=?"
            params.append(receipt_id)
        sql += " ORDER BY created_at DESC, row_index"
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


@router.get("/receipts")
def list_receipts(period_id: str | None = None):
    conn = connect()
    try:
        if period_id:
            rows = conn.execute(
                "SELECT * FROM load_receipts WHERE period_id=? ORDER BY created_at DESC",
                (period_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM load_receipts ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
