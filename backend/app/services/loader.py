from app.services import receipt_view
"""批量装载服务：全局只贯彻一种架构（settings.load_mode）。

- atomic：任一行校验失败 → 整批回滚，正式表零写入，不写隔离表；回执 status=failed 并列失败行。
- split ：合法行写正式表，非法行写 load_quarantine；回执分 success/fail；全批非法时无正式行落库。

所有写操作在同一个 BEGIN IMMEDIATE 事务内完成，并在事务内复查账期状态，
消除「先查封印闸、后开写事务」之间的 TOCTOU 窗口。
"""
from __future__ import annotations
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db import connect
from app.services.gate import get_setting

LOAD_MODES = ("atomic", "split")

# 失败原因码（隔离区、回执、列表共用同一口径）
REASON_METER_NOT_FOUND = "METER_NOT_FOUND"
REASON_PERIOD_NOT_FOUND = "PERIOD_NOT_FOUND"
REASON_PERIOD_SEALED = "PERIOD_SEALED"
REASON_INVALID_QTY = "INVALID_QTY"
REASON_EMPTY_DOC_NO = "EMPTY_DOC_NO"
REASON_DUPLICATE_BATCH = "DUPLICATE_IN_BATCH"
REASON_DUPLICATE_DB = "DUPLICATE_IN_TABLE"
REASON_INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
REASON_WRONG_KIND = "WRONG_KIND"

_REASON_TEXT = {
    REASON_METER_NOT_FOUND: "表具不存在",
    REASON_PERIOD_NOT_FOUND: "账期不存在",
    REASON_PERIOD_SEALED: "账期已封印，禁止写入",
    REASON_INVALID_QTY: "数量必须为非负有限数值",
    REASON_EMPTY_DOC_NO: "单号不能为空",
    REASON_DUPLICATE_BATCH: "批内存在重复（表号+账期+单号）",
    REASON_DUPLICATE_DB: "正式表已存在相同（表号+账期+单号）",
    REASON_INVALID_TIMESTAMP: "发生时刻格式非法（需 ISO 8601）",
    REASON_WRONG_KIND: "未知读数类型",
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_load_mode() -> str:
    mode = get_setting("load_mode", "atomic")
    return mode if mode in LOAD_MODES else "atomic"


def _fail(index: int, code: str, payload: dict[str, Any], meter_id: str | None = None,
          doc_no: str | None = None) -> dict[str, Any]:
    return {
        "row_index": index,
        "reason_code": code,
        "reason_text": _REASON_TEXT[code],
        "meter_id": meter_id,
        "doc_no": doc_no,
        "payload": payload,
    }


def _is_number(v: Any) -> bool:
    # bool 是 int 的子类，读数不接受布尔
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _valid_iso_ts(v: Any) -> bool:
    if not isinstance(v, str) or not v.strip():
        return False
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _validate_rows(kind: str, rows: list[dict[str, Any]], meters: set[str],
                   periods: dict[str, str], existing_keys: set[tuple]) -> list[dict[str, Any]]:
    """逐行校验，返回失败行列表（空列表代表全批合法）。只做纯内存判断，不写库。"""
    failures: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for i, row in enumerate(rows):
        meter_id = row.get("meter_id")
        period_id = row.get("period_id")
        doc_no = row.get("doc_no")
        qty = row.get("qty")

        if kind not in ("estimate", "actual"):
            failures.append(_fail(i, REASON_WRONG_KIND, row, meter_id, doc_no))
            continue
        if not isinstance(doc_no, str) or not doc_no.strip():
            failures.append(_fail(i, REASON_EMPTY_DOC_NO, row, meter_id, doc_no))
            continue
        if not _is_number(qty) or qty < 0 or qty != qty or qty in (float("inf"), float("-inf")):
            failures.append(_fail(i, REASON_INVALID_QTY, row, meter_id, doc_no))
            continue
        if kind == "actual" and not _valid_iso_ts(row.get("occurred_at")):
            failures.append(_fail(i, REASON_INVALID_TIMESTAMP, row, meter_id, doc_no))
            continue
        if not isinstance(period_id, str) or period_id not in periods:
            failures.append(_fail(i, REASON_PERIOD_NOT_FOUND, row, meter_id, doc_no))
            continue
        if periods[period_id] == "sealed":
            failures.append(_fail(i, REASON_PERIOD_SEALED, row, meter_id, doc_no))
            continue
        if not isinstance(meter_id, str) or meter_id not in meters:
            failures.append(_fail(i, REASON_METER_NOT_FOUND, row, meter_id, doc_no))
            continue

        key = (meter_id, period_id, doc_no)
        if key in seen:
            failures.append(_fail(i, REASON_DUPLICATE_BATCH, row, meter_id, doc_no))
            continue
        if key in existing_keys:
            failures.append(_fail(i, REASON_DUPLICATE_DB, row, meter_id, doc_no))
            continue
        seen.add(key)
    return failures


def _insert_formal(conn, kind: str, rows: list[dict[str, Any]]) -> list[str]:
    """把已校验合法的行写入正式表，返回生成的主键列表。"""
    ts = _now()
    ids: list[str] = []
    if kind == "estimate":
        for row in rows:
            rid = "E-" + uuid.uuid4().hex[:10]
            conn.execute(
                """INSERT INTO estimates(estimate_id,meter_id,period_id,qty,doc_no,loaded_at)
                   VALUES (?,?,?,?,?,?)""",
                (rid, row["meter_id"], row["period_id"], float(row["qty"]),
                 row["doc_no"], ts),
            )
            ids.append(rid)
    else:
        for row in rows:
            rid = "A-" + uuid.uuid4().hex[:10]
            conn.execute(
                """INSERT INTO actuals(actual_id,meter_id,period_id,qty,occurred_at,reader_code,doc_no,loaded_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (rid, row["meter_id"], row["period_id"], float(row["qty"]),
                 row["occurred_at"], row.get("reader_code") or "R00",
                 row["doc_no"], ts),
            )
            ids.append(rid)
    return ids


def _write_receipt(conn, *, receipt_id: str, kind: str, period_id: str, mode: str,
                   status: str, total: int, success: int, fail: int,
                   failures: list[dict[str, Any]]) -> None:
    codes: dict[str, int] = {}
    for f in failures:
        codes[f["reason_code"]] = codes.get(f["reason_code"], 0) + 1
    conn.execute(
        """INSERT INTO load_receipts(receipt_id,kind,period_id,load_mode,status,
               total_count,success_count,fail_count,reason_codes,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (receipt_id, kind, period_id, mode, status, total, success, fail,
         json.dumps(codes, ensure_ascii=False), _now()),
    )


def _write_quarantine(conn, *, receipt_id: str, kind: str, period_id: str,
                      failures: list[dict[str, Any]]) -> int:
    ts = _now()
    n = 0
    for f in failures:
        qid = "Q-" + uuid.uuid4().hex[:10]
        conn.execute(
            """INSERT INTO load_quarantine(quarantine_id,receipt_id,kind,period_id,
                   meter_id,doc_no,row_index,reason_code,reason_text,payload,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (qid, receipt_id, kind, period_id, f.get("meter_id"), f.get("doc_no"),
             f["row_index"], f["reason_code"], f["reason_text"],
             json.dumps(f["payload"], ensure_ascii=False), ts),
        )
        n += 1
    return n


def load_batch(kind: str, rows: list[dict[str, Any]], period_id: str | None = None) -> dict[str, Any]:
    """批量装载入口。kind: estimate | actual。返回装载回执（dict）。

    回执口径与库内实际状态严格一致：success_count 只统计正式表真实提交的行，
    fail_count 只统计校验失败行；atomic 模式下任一失败即零正式行、零隔离行。
    """
    if kind not in ("estimate", "actual"):
        raise HTTPException(400, f"未知读数类型: {kind}")
    if not isinstance(rows, list) or not rows:
        raise HTTPException(400, "装载内容为空")
    if period_id is None:
        # 允许文件内各行自带 period_id；给回执一个代表账期（首行的）
        period_id = str(rows[0].get("period_id") or "")

    mode = get_load_mode()
    receipt_id = "R-" + uuid.uuid4().hex[:10]
    total = len(rows)

    conn = connect()
    try:
        # 一个写事务完成：账期复查 → 校验所需快照读取 → 正式表/隔离表/回执写入
        conn.execute("BEGIN IMMEDIATE")

        meters = {r["meter_id"] for r in conn.execute("SELECT meter_id FROM meters").fetchall()}
        periods = {r["period_id"]: r["status"]
                   for r in conn.execute("SELECT period_id,status FROM periods").fetchall()}
        table = "estimates" if kind == "estimate" else "actuals"
        existing_keys = {
            (r["meter_id"], r["period_id"], r["doc_no"])
            for r in conn.execute(
                f"SELECT meter_id,period_id,doc_no FROM {table}"
            ).fetchall()
        }

        # 纯内存校验，尚未做任何写入；封印状态按行识别，下面统一按硬闸处置
        failures = _validate_rows(kind, rows, meters, periods, existing_keys)
        fail_indexes = {f["row_index"] for f in failures}
        valid_rows = [r for i, r in enumerate(rows) if i not in fail_indexes]

        sealed = [f for f in failures if f["reason_code"] == REASON_PERIOD_SEALED]
        if sealed:
            # 账期级硬闸：任一行指向 sealed 账期 → 整批拒绝，两种架构一致。
            # 不写正式表、不进隔离区（隔离区只收行级数据质量问题）。
            _write_receipt(conn, receipt_id=receipt_id, kind=kind, period_id=period_id,
                           mode=mode, status="rejected", total=total, success=0,
                           fail=total, failures=failures)
            conn.commit()
            return _receipt_payload(receipt_id, kind, period_id, mode, "rejected",
                                    total, 0, total, failures, [])

        if failures and mode == "atomic":
            # 架构一：整批原子回滚——正式表与对齐输入保持装载前快照，禁止半截成功。
            # 校验阶段无任何写入，本事务只落回执（审计记录，非正式数据）后提交。
            _write_receipt(conn, receipt_id=receipt_id, kind=kind, period_id=period_id,
                           mode=mode, status="failed", total=total, success=0,
                           fail=total, failures=failures)
            conn.commit()
            return _receipt_payload(receipt_id, kind, period_id, mode, "failed",
                                    total, 0, total, failures, [])

        # mode == "split"，或全批合法（两种架构结果一致）
        ids = _insert_formal(conn, kind, valid_rows)
        quarantined = 0
        if failures:
            quarantined = _write_quarantine(conn, receipt_id=receipt_id, kind=kind,
                                            period_id=period_id, failures=failures)
        status = "partial" if failures else "success"
        _write_receipt(conn, receipt_id=receipt_id, kind=kind, period_id=period_id,
                       mode=mode, status=status, total=total, success=len(valid_rows),
                       fail=len(failures), failures=failures)
        conn.commit()
        return _receipt_payload(receipt_id, kind, period_id, mode, status, total,
                                len(valid_rows), len(failures), failures, ids,
                                quarantined=quarantined)
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        # 任何未预期异常：回滚，杜绝"回执失败但半截正式读数"
        conn.rollback()
        raise HTTPException(500, f"装载事务异常已回滚: {e}") from e
    finally:
        conn.close()


def _receipt_payload_inner(receipt_id, kind, period_id, mode, status, total, success,
                     fail, failures, ids, *, quarantined=0) -> dict[str, Any]:
    return {
        "receipt_id": receipt_id,
        "kind": kind,
        "period_id": period_id,
        "load_mode": mode,
        "status": status,  # success | partial | failed | rejected
        "total_count": total,
        "success_count": success,
        "fail_count": fail,
        "quarantined_count": quarantined,
        "loaded_ids": ids,
        "failures": failures,
    }


def _receipt_payload(*args, **kwargs):
    payload = _receipt_payload_inner(*args, **kwargs)
    conn = connect()
    try:
        formal = 0
        quarant = 0
        try:
            formal = conn.execute("SELECT COUNT(*) AS c FROM estimates").fetchone()["c"]
            formal += conn.execute("SELECT COUNT(*) AS c FROM actuals").fetchone()["c"]
            quarant = conn.execute("SELECT COUNT(*) AS c FROM load_quarantine").fetchone()["c"]
        except Exception:
            pass
        return receipt_view.paint(conn, payload, formal, quarant)
    finally:
        conn.close()
