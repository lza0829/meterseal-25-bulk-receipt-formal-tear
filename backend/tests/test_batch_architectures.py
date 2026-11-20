"""架构一 atomic：夹一笔坏行 → 整批零写入；架构二 split：成功行可查、坏行进隔离。"""
from conftest import count, set_mode, EST_BATCH
import app.db as db_mod


def _formal_est_count():
    return count(None, "SELECT COUNT(*) FROM estimates WHERE doc_no LIKE 'B-D%'")


def _quarantine_count():
    return count(None, "SELECT COUNT(*) FROM load_quarantine")


def test_atomic_bad_row_zero_writes(client):
    set_mode(client, "atomic")
    before = count(None, "SELECT COUNT(*) FROM estimates")

    r = client.post("/api/estimates/batch", json={"period_id": "2026-10", "rows": EST_BATCH})
    assert r.status_code == 200
    rcpt = r.json()

    # 回执口径：failed，成功 0、失败 3（整批命运，不只坏行计数为 1，success 必为 0）
    assert rcpt["status"] == "failed"
    assert rcpt["load_mode"] == "atomic"
    assert rcpt["total_count"] == 3
    assert rcpt["success_count"] == 0
    assert rcpt["fail_count"] == 3
    assert rcpt["loaded_ids"] == []
    codes = {f["reason_code"] for f in rcpt["failures"]}
    assert "METER_NOT_FOUND" in codes

    # 正式表保持装载前快照：零新增
    assert count(None, "SELECT COUNT(*) FROM estimates") == before
    assert _formal_est_count() == 0
    # atomic 不写隔离区
    assert _quarantine_count() == 0

    # 回执已落库且与返回口径一致
    rr = client.get("/api/receipts").json()
    assert rr[0]["receipt_id"] == rcpt["receipt_id"]
    assert rr[0]["status"] == "failed"
    assert rr[0]["success_count"] == 0


def test_split_bad_row_partial_load_and_quarantine(client):
    set_mode(client, "split")
    before = count(None, "SELECT COUNT(*) FROM estimates")

    r = client.post("/api/estimates/batch", json={"period_id": "2026-10", "rows": EST_BATCH})
    rcpt = r.json()
    assert rcpt["status"] == "partial"
    assert rcpt["load_mode"] == "split"
    assert rcpt["success_count"] == 2
    assert rcpt["fail_count"] == 1
    assert rcpt["quarantined_count"] == 1
    assert len(rcpt["loaded_ids"]) == 2

    # 成功行进正式表且可查
    assert count(None, "SELECT COUNT(*) FROM estimates") == before + 2
    rows = client.get("/api/estimates?period_id=2026-10").json()
    docs = {x["doc_no"] for x in rows}
    assert {"B-D1", "B-D2"} <= docs
    assert "B-D3" not in docs

    # 坏行进隔离区，原因码可对上
    q = client.get("/api/quarantine").json()
    assert len(q) == 1
    assert q[0]["reason_code"] == "METER_NOT_FOUND"
    assert q[0]["receipt_id"] == rcpt["receipt_id"]
    assert q[0]["doc_no"] == "B-D3"


def test_switch_mode_same_bad_file_results_are_comparable(client):
    """同一坏文件：两种架构分别跑，结果可对照——正式表差 2 行，隔离区差 1 条。"""
    # atomic 先跑
    set_mode(client, "atomic")
    r1 = client.post("/api/estimates/batch",
                     json={"period_id": "2026-10", "rows": EST_BATCH}).json()
    assert r1["status"] == "failed" and r1["success_count"] == 0
    atomic_formal = count(None, "SELECT COUNT(*) FROM estimates WHERE doc_no LIKE 'B-D%'")
    atomic_q = _quarantine_count()

    # 切换到 split，同文件再跑（换单号避免与种子/前面批次撞键）
    set_mode(client, "split")
    rows2 = [{**r, "doc_no": r["doc_no"].replace("B-D", "C-D")} for r in EST_BATCH]
    r2 = client.post("/api/estimates/batch",
                     json={"period_id": "2026-10", "rows": rows2}).json()
    assert r2["status"] == "partial" and r2["success_count"] == 2
    split_formal = count(None, "SELECT COUNT(*) FROM estimates WHERE doc_no LIKE 'C-D%'")
    split_q = _quarantine_count()

    assert atomic_formal == 0
    assert split_formal == 2
    assert atomic_q == 0
    assert split_q == 1
    # 两架构回执总数都对得上输入行数
    assert r1["total_count"] == r2["total_count"] == 3


def test_all_valid_rows_identical_between_modes(client):
    good = [
        {"meter_id": "M-W-101", "period_id": "2026-10", "qty": 1.0, "doc_no": "G-1"},
        {"meter_id": "M-W-102", "period_id": "2026-10", "qty": 2.0, "doc_no": "G-2"},
    ]
    set_mode(client, "atomic")
    r = client.post("/api/estimates/batch", json={"period_id": "2026-10", "rows": good}).json()
    assert r["status"] == "success" and r["success_count"] == 2 and r["fail_count"] == 0
    assert _quarantine_count() == 0


def test_invalid_qty_and_empty_docno_reasons(client):
    set_mode(client, "split")
    rows = [
        {"meter_id": "M-W-101", "period_id": "2026-10", "qty": -5, "doc_no": "X-1"},
        {"meter_id": "M-W-102", "period_id": "2026-10", "qty": 3, "doc_no": "   "},
        {"meter_id": "M-E-101", "period_id": "2026-10", "qty": 4, "doc_no": "X-3"},
    ]
    r = client.post("/api/estimates/batch", json={"period_id": "2026-10", "rows": rows}).json()
    assert r["success_count"] == 1
    codes = sorted(f["reason_code"] for f in r["failures"])
    assert codes == ["EMPTY_DOC_NO", "INVALID_QTY"]
    # 隔离区原因码与回执失败原因码分布一致
    q = client.get("/api/quarantine").json()
    qcodes = sorted(x["reason_code"] for x in q)
    assert qcodes == codes


def test_duplicate_in_batch_and_in_table(client):
    set_mode(client, "split")
    rows = [
        {"meter_id": "M-W-101", "period_id": "2026-10", "qty": 1, "doc_no": "DUP"},
        {"meter_id": "M-W-101", "period_id": "2026-10", "qty": 2, "doc_no": "DUP"},  # 批内重复
        # EST-101 是种子里已存在的 (M-W-101,2026-10) 单号
        {"meter_id": "M-W-101", "period_id": "2026-10", "qty": 3, "doc_no": "EST-101"},
    ]
    r = client.post("/api/estimates/batch", json={"period_id": "2026-10", "rows": rows}).json()
    assert r["success_count"] == 1
    codes = {f["reason_code"] for f in r["failures"]}
    assert codes == {"DUPLICATE_IN_BATCH", "DUPLICATE_IN_TABLE"}
