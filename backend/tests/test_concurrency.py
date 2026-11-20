"""并发两批装载：串行化写事务，不得交叉污染或留下半截正式行；回执与库笔数对账。"""
import threading

from conftest import count, set_mode


def _post_batch(client_holder, kind, rows, period="2026-09"):
    # 每个线程自己的 TestClient（底层各自连接）
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    url = f"/api/{kind}/batch"
    r = c.post(url, json={"period_id": period, "rows": rows})
    client_holder.append(r.json())


def test_concurrent_batches_no_cross_contamination_split(client):
    set_mode(client, "split")
    # 两批各 5 行，全部合法，单号互不撞；账期 2026-09 为空账期
    batch_a = [
        {"meter_id": f"M-W-10{(i % 2) + 1}", "period_id": "2026-09",
         "qty": 10 + i, "doc_no": f"CA-{i}"}
        for i in range(5)
    ]
    batch_b = [
        {"meter_id": f"M-E-20{(i % 2) + 1}", "period_id": "2026-09",
         "qty": 20 + i, "doc_no": f"CB-{i}"}
        for i in range(5)
    ]
    out: list = []
    t1 = threading.Thread(target=_post_batch, args=(out, "estimates", batch_a))
    t2 = threading.Thread(target=_post_batch, args=(out, "estimates", batch_b))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert len(out) == 2
    for rcpt in out:
        assert rcpt["status"] == "success"
        assert rcpt["success_count"] == 5
        assert rcpt["fail_count"] == 0

    # 正式表恰好 10 行，没有任何行丢失或串号
    formal = count(None,
                   "SELECT COUNT(*) FROM estimates WHERE period_id='2026-09' AND doc_no LIKE 'C%'")
    assert formal == 10
    docs = {r["doc_no"] for r in
            client.get("/api/estimates?period_id=2026-09").json()}
    assert {f"CA-{i}" for i in range(5)} <= docs
    assert {f"CB-{i}" for i in range(5)} <= docs
    assert count(None, "SELECT COUNT(*) FROM load_quarantine") == 0


def test_concurrent_batches_with_bad_rows_atomic(client):
    """atomic 下并发夹坏行：每批要么整批 0 行要么整批 5 行，绝不出现半截。"""
    set_mode(client, "atomic")
    good_batch = [
        {"meter_id": "M-W-101", "period_id": "2026-09",
         "qty": i + 1, "doc_no": f"GA-{i}"}
        for i in range(5)
    ]
    bad_batch = [
        {"meter_id": "M-W-102", "period_id": "2026-09",
         "qty": i + 1, "doc_no": f"GB-{i}"}
        for i in range(5)
    ]
    bad_batch[3]["meter_id"] = "M-GHOST"  # 夹一笔坏行

    out: list = []
    t1 = threading.Thread(target=_post_batch, args=(out, "estimates", good_batch))
    t2 = threading.Thread(target=_post_batch, args=(out, "estimates", bad_batch))
    t1.start(); t2.start(); t1.join(); t2.join()

    by_status = {r["status"]: r for r in out}
    assert "success" in by_status and "failed" in by_status
    assert by_status["success"]["success_count"] == 5
    assert by_status["failed"]["success_count"] == 0

    # 好批 5 行整批可见；坏批零行——不存在 1~4 行的半截状态
    ga = count(None,
               "SELECT COUNT(*) FROM estimates WHERE period_id='2026-09' AND doc_no LIKE 'GA-%'")
    gb = count(None,
               "SELECT COUNT(*) FROM estimates WHERE period_id='2026-09' AND doc_no LIKE 'GB-%'")
    assert ga == 5 and gb == 0


def test_receipt_totals_reconcile_with_table_counts(client):
    """回执 success 累计 = 正式表本账期新增；fail 累计 = 隔离区行数（split）。"""
    set_mode(client, "split")
    rows_a = [
        {"meter_id": "M-W-101", "period_id": "2026-09", "qty": 1, "doc_no": "T-1"},
        {"meter_id": "M-GONE", "period_id": "2026-09", "qty": 1, "doc_no": "T-2"},
    ]
    rows_b = [
        {"meter_id": "M-W-102", "period_id": "2026-09", "qty": 2, "doc_no": "T-3"},
        {"meter_id": "M-E-101", "period_id": "2026-09", "qty": 2, "doc_no": "T-4"},
    ]
    client.post("/api/estimates/batch", json={"period_id": "2026-09", "rows": rows_a})
    client.post("/api/estimates/batch", json={"period_id": "2026-09", "rows": rows_b})

    ov = client.get("/api/overview").json()
    formal_09 = count(None,
                      "SELECT COUNT(*) FROM estimates WHERE period_id='2026-09'")
    assert formal_09 == 3  # 1 + 2
    assert ov["quarantine_count"] == 1
    assert ov["quarantine_reasons"].get("METER_NOT_FOUND") == 1
    # 回执汇总口径与正式表/隔离区对得上
    assert ov["receipt_success_total"] >= 3
    assert ov["receipt_fail_total"] >= 1

    receipts = client.get("/api/receipts?period_id=2026-09").json()
    s = sum(r["success_count"] for r in receipts)
    f = sum(r["fail_count"] for r in receipts)
    assert s == formal_09
    assert f == ov["quarantine_count"]
