"""封印闸：已 sealed 账期的批量/单行写入在两种架构下都被拒，且无任何落库。"""
from conftest import count, set_mode


def _seal(client, period="2026-10"):
    r = client.post("/api/align", json={"period_id": period})
    assert r.status_code == 200, r.text
    r = client.post("/api/seal", json={"period_id": period})
    assert r.status_code == 200, r.text


def test_sealed_period_batch_rejected_atomic(client):
    set_mode(client, "atomic")
    _seal(client)
    before = count(None, "SELECT COUNT(*) FROM estimates")
    rows = [
        {"meter_id": "M-W-101", "period_id": "2026-10", "qty": 9, "doc_no": "S-1"},
        {"meter_id": "M-W-102", "period_id": "2026-10", "qty": 9, "doc_no": "S-2"},
    ]
    r = client.post("/api/estimates/batch", json={"period_id": "2026-10", "rows": rows})
    rcpt = r.json()
    assert rcpt["status"] == "rejected"
    assert rcpt["success_count"] == 0
    assert all(f["reason_code"] == "PERIOD_SEALED" for f in rcpt["failures"])
    # 正式表零变化，隔离区也不收封印行
    assert count(None, "SELECT COUNT(*) FROM estimates") == before
    assert count(None, "SELECT COUNT(*) FROM load_quarantine") == 0


def test_sealed_period_batch_rejected_split(client):
    set_mode(client, "split")
    _seal(client)
    before = count(None, "SELECT COUNT(*) FROM actuals")
    rows = [
        {"meter_id": "M-W-101", "period_id": "2026-10", "qty": 9,
         "occurred_at": "2026-10-29T10:00:00", "doc_no": "SA-1"},
    ]
    rcpt = client.post("/api/actuals/batch",
                       json={"period_id": "2026-10", "rows": rows}).json()
    assert rcpt["status"] == "rejected"
    assert rcpt["success_count"] == 0
    assert count(None, "SELECT COUNT(*) FROM actuals") == before
    assert count(None, "SELECT COUNT(*) FROM load_quarantine") == 0


def test_sealed_period_single_row_still_rejected(client):
    set_mode(client, "split")
    _seal(client)
    r = client.post("/api/estimates", json={
        "meter_id": "M-W-101", "period_id": "2026-10", "qty": 1, "doc_no": "SINGLE-S"})
    assert r.status_code == 409


def test_align_rejected_after_seal(client):
    _seal(client)
    r = client.post("/api/align", json={"period_id": "2026-10"})
    assert r.status_code == 409


def test_invalid_load_mode_setting_rejected(client):
    r = client.put("/api/settings/load_mode", json={"value": "chaos"})
    assert r.status_code == 400
    # 开关仍为缺省合法值
    assert client.get("/api/settings").json()["load_mode"] == "atomic"


def test_mixed_period_sealed_row_rejects_whole_batch_even_in_split(client):
    """split 下夹一行指向 sealed 账期：整批 rejected，不进隔离、不写正式表。"""
    set_mode(client, "split")
    _seal(client)  # 封印 2026-10；2026-09 仍开放
    before_open = count(None, "SELECT COUNT(*) FROM estimates WHERE period_id='2026-09'")
    rows = [
        {"meter_id": "M-W-101", "period_id": "2026-09", "qty": 1, "doc_no": "MIX-OK"},
        {"meter_id": "M-W-102", "period_id": "2026-10", "qty": 1, "doc_no": "MIX-SEALED"},
    ]
    rcpt = client.post("/api/estimates/batch",
                       json={"rows": rows}).json()
    assert rcpt["status"] == "rejected"
    assert rcpt["success_count"] == 0
    assert any(f["reason_code"] == "PERIOD_SEALED" for f in rcpt["failures"])
    # 开放账期的合法行也不得落库（整批命运），隔离区为空
    assert count(None, "SELECT COUNT(*) FROM estimates WHERE period_id='2026-09'") == before_open
    assert count(None, "SELECT COUNT(*) FROM load_quarantine") == 0


def test_unknown_period_row_split_goes_quarantine(client):
    set_mode(client, "split")
    rows = [
        {"meter_id": "M-W-101", "period_id": "2026-09", "qty": 1, "doc_no": "P-OK"},
        {"meter_id": "M-W-102", "period_id": "2099-99", "qty": 1, "doc_no": "P-BAD"},
    ]
    rcpt = client.post("/api/estimates/batch", json={"rows": rows}).json()
    assert rcpt["status"] == "partial"
    assert rcpt["success_count"] == 1
    q = client.get("/api/quarantine").json()
    assert len(q) == 1 and q[0]["reason_code"] == "PERIOD_NOT_FOUND"
