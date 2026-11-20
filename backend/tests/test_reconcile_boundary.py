"""对齐物化：隔离行不得进 matched；matched+variance 与正式表笔数口径一致。"""
from conftest import count, set_mode


def test_quarantined_rows_never_align(client):
    set_mode(client, "split")
    # 2026-09 空账期：一笔合法实抄 + 一笔坏实抄（表具不存在）
    rows = [
        {"meter_id": "M-W-101", "period_id": "2026-09", "qty": 50.0,
         "occurred_at": "2026-09-28T10:00:00", "doc_no": "QA-1"},
        {"meter_id": "M-GHOST", "period_id": "2026-09", "qty": 60.0,
         "occurred_at": "2026-09-28T10:05:00", "doc_no": "QA-BAD"},
    ]
    rcpt = client.post("/api/actuals/batch",
                       json={"period_id": "2026-09", "rows": rows}).json()
    assert rcpt["success_count"] == 1 and rcpt["fail_count"] == 1

    # 配套一笔估抄使合法行可 matched
    client.post("/api/estimates/batch", json={
        "period_id": "2026-09",
        "rows": [{"meter_id": "M-W-101", "period_id": "2026-09",
                  "qty": 50.0, "doc_no": "QE-1"}],
    })

    align = client.post("/api/align", json={"period_id": "2026-09"}).json()
    # 隔离的 M-GHOST 既不 matched 也不 variance
    assert align["matched_count"] == 1
    assert align["variance_count"] == 0

    variances = client.get("/api/variances?period_id=2026-09").json()
    assert all(v["meter_id"] != "M-GHOST" for v in variances)

    # 对账批笔数 = 正式表去重表具数，与隔离区无关
    formal_meters = count(
        None,
        "SELECT COUNT(DISTINCT meter_id) FROM ("
        "SELECT meter_id FROM estimates WHERE period_id='2026-09' "
        "UNION SELECT meter_id FROM actuals WHERE period_id='2026-09')")
    assert align["matched_count"] + align["variance_count"] == formal_meters


def test_actual_batch_bad_timestamp_quarantined(client):
    set_mode(client, "split")
    rows = [
        {"meter_id": "M-W-101", "period_id": "2026-09", "qty": 1,
         "occurred_at": "not-a-time", "doc_no": "TS-BAD"},
        {"meter_id": "M-W-102", "period_id": "2026-09", "qty": 2,
         "occurred_at": "2026-09-28T10:00:00", "doc_no": "TS-OK"},
    ]
    rcpt = client.post("/api/actuals/batch",
                       json={"period_id": "2026-09", "rows": rows}).json()
    assert rcpt["success_count"] == 1
    assert rcpt["failures"][0]["reason_code"] == "INVALID_TIMESTAMP"
    q = client.get("/api/quarantine").json()
    assert q[0]["reason_code"] == "INVALID_TIMESTAMP"
