"""批量装载服务：全局只贯彻一种架构（settings.load_mode）。

架构一（以正式∪隔离行集合为准，全系统唯一口径）：
- atomic：任一行校验失败 → 整批回滚，正式表零写入，不写隔离表；回执 status=failed 并列失败行。
- split ：合法行写正式表，非法行写 load_quarantine；回执分 success/fail；全批非法时无正式行落库。
- 账期封印硬闸：任一行指向 sealed 账期 → 两种架构都整批 rejected，不写正式表也不进隔离。

回执不是独立事实源，而是落库集合的审计投影。每个分支提交前都执行
_replay_receipt：用本批 receipt_id 回查正式表/隔离表，重放 success/fail 键集合，
与回执声明逐项比对。任何一项不符即抛异常 → 整事务回滚，杜绝半截正式行与幽灵回执。

所有写操作在同一个 BEGIN IMMEDIATE 事务内完成，并在事务内复查账期状态与装载模式，
消除「先查开关/封印闸、后开写事务」之间的 TOCTOU 窗口；写事务串行化，并发两批互不污染。
"""
from __future__ import annotations
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db import connect

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


class _ReplayMismatch(RuntimeError):
    """落库重放与回执声明不一致——架构一的不变量被破坏，调用方必须回滚整事务。"""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_mode_in_tx(conn) -> str:
    """装载模式在写事务内读取：拿锁后的快照决定整批命运，切换开关无法穿透进行中的事务。"""
    row = conn.execute("SELECT value FROM settings WHERE key='load_mode'").fetchone()
    mode = row["value"] if row else "atomic"
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


def _insert_formal(conn, kind: str, rows: list[dict[str, Any]], receipt_id: str) -> list[str]:
    """把已校验合法的行写入正式表并盖 receipt_id 溯源章，返回生成的主键列表。"""
    ts = _now()
    ids: list[str] = []
    if kind == "estimate":
        for row in rows:
            rid = "E-" + uuid.uuid4().hex[:10]
            conn.execute(
                """INSERT INTO estimates(estimate_id,meter_id,period_id,qty,doc_no,loaded_at,receipt_id)
                   VALUES (?,?,?,?,?,?,?)""",
                (rid, row["meter_id"], row["period_id"], float(row["qty"]),
                 row["doc_no"], ts, receipt_id),
            )
            ids.append(rid)
    else:
        for row in rows:
            rid = "A-" + uuid.uuid4().hex[:10]
            conn.execute(
                """INSERT INTO actuals(actual_id,meter_id,period_id,qty,occurred_at,reader_code,doc_no,loaded_at,receipt_id)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (rid, row["meter_id"], row["period_id"], float(row["qty"]),
                 row["occurred_at"], row.get("reader_code") or "R00",
                 row["doc_no"], ts, receipt_id),
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


def _replay_receipt(conn, *, receipt_id: str, kind: str, mode: str, status: str,
                    total: int, success: int, fail: int,
                    expected_success_keys: set[tuple],
                    expected_quarantine_indexes: set[int]) -> None:
    """架构一闭环：以正式∪隔离行集合重放回执，任何不符即抛 _ReplayMismatch。

    - 回执行存在且字段一致；
    - 正式表中盖本章的行键集合 == 回执宣称成功键集合（缺行/幽灵行都不允许）；
    - 隔离区中本章行 == 应隔离行（split 失败行进隔离；atomic/rejected 不进隔离，
      row_index 集合必须为空——隔离多报/少报都不允许）；
    - 笔数恒等式 total == success + fail；
    - failed/rejected 回执不得在正式表留下任何行（禁止回执失败却正式有行）。
    """
    table = "estimates" if kind == "estimate" else "actuals"
    receipt = conn.execute(
        "SELECT * FROM load_receipts WHERE receipt_id=?", (receipt_id,)
    ).fetchone()
    if receipt is None:
        raise _ReplayMismatch(f"回执未落库: {receipt_id}")
    if (receipt["status"] != status or receipt["load_mode"] != mode
            or receipt["total_count"] != total
            or receipt["success_count"] != success
            or receipt["fail_count"] != fail):
        raise _ReplayMismatch(
            f"回执字段与落库声明不符: db={dict(receipt)} vs "
            f"claim=(status={status},mode={mode},total={total},ok={success},fail={fail})"
        )
    if total != success + fail:
        raise _ReplayMismatch(f"回执笔数不闭合: total={total} ok={success} fail={fail}")

    formal_rows = conn.execute(
        f"SELECT meter_id,period_id,doc_no FROM {table} WHERE receipt_id=?",
        (receipt_id,),
    ).fetchall()
    formal_keys = {(r["meter_id"], r["period_id"], r["doc_no"]) for r in formal_rows}
    if len(formal_rows) != len(formal_keys):
        raise _ReplayMismatch("正式表本批出现重复键，唯一约束口径被破坏")
    if formal_keys != expected_success_keys:
        raise _ReplayMismatch(
            f"正式行键集合重放不符: missing={sorted(expected_success_keys - formal_keys)}, "
            f"ghost={sorted(formal_keys - expected_success_keys)}"
        )
    if len(formal_keys) != success:
        raise _ReplayMismatch(
            f"回执成功 {success} 与正式落库 {len(formal_keys)} 笔不一致"
        )

    q_rows = conn.execute(
        "SELECT row_index FROM load_quarantine WHERE receipt_id=?", (receipt_id,)
    ).fetchall()
    q_indexes = {r["row_index"] for r in q_rows}
    if q_indexes != expected_quarantine_indexes:
        raise _ReplayMismatch(
            f"隔离行重放不符: missing={sorted(expected_quarantine_indexes - q_indexes)}, "
            f"over_report={sorted(q_indexes - expected_quarantine_indexes)}"
        )
    expected_q_count = len(expected_quarantine_indexes)
    if len(q_indexes) != expected_q_count:
        raise _ReplayMismatch(f"隔离应落 {expected_q_count} 笔，实际 {len(q_indexes)} 笔")

    if status in ("failed", "rejected") and formal_keys:
        raise _ReplayMismatch("失败/拒收回执不得在正式表留下任何行")


def load_batch(kind: str, rows: list[dict[str, Any]], period_id: str | None = None) -> dict[str, Any]:
    """批量装载入口。kind: estimate | actual。返回装载回执（dict）。

    回执口径与库内实际状态严格一致：success_count 只统计正式表真实提交的行，
    fail_count 只统计校验失败行；atomic 模式下任一失败即零正式行、零隔离行。
    提交前以落库集合重放回执，重放不符整事务回滚，绝不留下半截状态。
    """
    if kind not in ("estimate", "actual"):
        raise HTTPException(400, f"未知读数类型: {kind}")
    if not isinstance(rows, list) or not rows:
        raise HTTPException(400, "装载内容为空")
    if period_id is None:
        # 允许文件内各行自带 period_id；给回执一个代表账期（首行的）
        period_id = str(rows[0].get("period_id") or "")

    receipt_id = "R-" + uuid.uuid4().hex[:10]
    total = len(rows)

    conn = connect()
    try:
        # 一个写事务完成：账期/模式复查 → 校验快照读取 → 正式表/隔离表/回执写入 → 落库重放
        conn.execute("BEGIN IMMEDIATE")
        mode = _load_mode_in_tx(conn)

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
        success_keys = {(r["meter_id"], r["period_id"], r["doc_no"]) for r in valid_rows}

        sealed = [f for f in failures if f["reason_code"] == REASON_PERIOD_SEALED]
        if sealed:
            # 账期级硬闸：任一行指向 sealed 账期 → 整批拒绝，两种架构一致。
            # 不写正式表、不进隔离区（隔离区只收行级数据质量问题）。
            _write_receipt(conn, receipt_id=receipt_id, kind=kind, period_id=period_id,
                           mode=mode, status="rejected", total=total, success=0,
                           fail=total, failures=failures)
            _replay_receipt(conn, receipt_id=receipt_id, kind=kind, mode=mode,
                            status="rejected", total=total, success=0, fail=total,
                            expected_success_keys=set(),
                            expected_quarantine_indexes=set())
            conn.commit()
            return _receipt_payload(receipt_id, kind, period_id, mode, "rejected",
                                    total, 0, total, failures, [])

        if failures and mode == "atomic":
            # 架构一：整批原子回滚——正式表与对齐输入保持装载前快照，禁止半截成功。
            # 校验阶段无任何写入，本事务只落回执（审计记录，非正式数据）后提交。
            _write_receipt(conn, receipt_id=receipt_id, kind=kind, period_id=period_id,
                           mode=mode, status="failed", total=total, success=0,
                           fail=total, failures=failures)
            _replay_receipt(conn, receipt_id=receipt_id, kind=kind, mode=mode,
                            status="failed", total=total, success=0, fail=total,
                            expected_success_keys=set(),
                            expected_quarantine_indexes=set())
            conn.commit()
            return _receipt_payload(receipt_id, kind, period_id, mode, "failed",
                                    total, 0, total, failures, [])

        # mode == "split"，或全批合法（两种架构结果一致）
        ids = _insert_formal(conn, kind, valid_rows, receipt_id)
        quarantined = 0
        if failures:
            quarantined = _write_quarantine(conn, receipt_id=receipt_id, kind=kind,
                                            period_id=period_id, failures=failures)
        status = "partial" if failures else "success"
        _write_receipt(conn, receipt_id=receipt_id, kind=kind, period_id=period_id,
                       mode=mode, status=status, total=total, success=len(valid_rows),
                       fail=len(failures), failures=failures)
        # 提交前闭环：正式∪隔离必须能重放回执，否则整批回滚（含已写半截正式行）
        _replay_receipt(conn, receipt_id=receipt_id, kind=kind, mode=mode,
                        status=status, total=total, success=len(valid_rows),
                        fail=len(failures), expected_success_keys=success_keys,
                        expected_quarantine_indexes=fail_indexes)
        conn.commit()
        return _receipt_payload(receipt_id, kind, period_id, mode, status, total,
                                len(valid_rows), len(failures), failures, ids,
                                quarantined=quarantined)
    except HTTPException:
        conn.rollback()
        raise
    except _ReplayMismatch as e:
        # 重放不符：回滚半截正式行与幽灵回执，回执与落库冲突时一律听落库
        conn.rollback()
        raise HTTPException(500, f"落库重放与回执不符，整批已回滚: {e}") from e
    except Exception as e:
        # 任何未预期异常：回滚，杜绝"回执失败但半截正式读数"
        conn.rollback()
        raise HTTPException(500, f"装载事务异常已回滚: {e}") from e
    finally:
        conn.close()


def _receipt_payload(receipt_id, kind, period_id, mode, status, total, success,
                     fail, failures, ids, *, quarantined=0) -> dict[str, Any]:
    # 回执由事务内已重放的落库事实直接构造，不再二次开连接、不做任何"粉刷"
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
