"""五链收口测试：回执 / 正式 / 隔离 / 总览 / 对齐输入必须以「正式∪隔离行集合」为准对齐。

夹具覆盖：
- 回执成功多于正式（模拟落库丢行）→ 整批回滚，正式零新增、回执不落；
- 整批失败已半截（模拟正式表被塞进半截行）→ 回滚，禁止回执失败却正式有行；
- 隔离多报（模拟隔离区多写）→ 回滚，隔离零新增；
- 交错进对齐的半截 → 半截被回滚后对齐只吃可解释正式行；
- 并发两批交叉污染 → 串行事务各自闭合；
- 健康检查/总览的五链重放对账。
"""
import threading

import app.services.loader as loader_mod
from conftest import count, set_mode

EST_BATCH = [
    {"meter_id": "M-W-101", "period_id": "2026-09", "qty": 100.0, "doc_no": "F-D1"},
    {"meter_id": "M-W-102", "period_id": "2026-09", "qty": 200.0, "doc_no": "F-D2"},
    {"meter_id": "M-E-101", "period_id": "2026-09", "qty": 300.0, "doc_no": "F-D3"},
]


def _formal_09():
    return count(None, "SELECT COUNT(*) FROM estimates WHERE period_id='2026-09'")


def _quarantine():
    return count(None, "SELECT COUNT(*) FROM load_quarantine")


def _receipt_rows():
    return count(None, "SELECT COUNT(*) FROM load_receipts")


def test_receipt_success_more_than_formal_rolls_back(client, monkeypatch):
    """夹具一：回执宣称成功 3，正式只落 2（模拟丢行）→ 重放不符，整批回滚并重写回执。"""
    set_mode(client, "split")
    before_formal = count(None, "SELECT COUNT(*) FROM estimates")
    before_receipts = _receipt_rows()

    real_insert = loader_mod._insert_formal

    def dropping_insert(conn, kind, rows, receipt_id):
        # 丢掉最后一行的真实落库，但回执仍按 3 行宣称成功
        return real_insert(conn, kind, rows[:-1], receipt_id)

    monkeypatch.setattr(loader_mod, "_insert_formal", dropping_insert)

    r = client.post("/api/estimates/batch", json={"period_id": "2026-09", "rows": EST_BATCH})
    assert r.status_code == 500
    assert "重放" in r.json()["detail"]

    # 半截正式行随事务回滚，回执也不落库（不允许幽灵回执）
    assert count(None, "SELECT COUNT(*) FROM estimates") == before_formal
    assert _formal_09() == 0
    assert _receipt_rows() == before_receipts
    assert _quarantine() == 0

    # 再用同一批数据正常装载：成功，且五链对齐
    monkeypatch.undo()
    ok = client.post("/api/estimates/batch", json={"period_id": "2026-09", "rows": EST_BATCH})
    assert ok.status_code == 200 and ok.json()["status"] == "success"
    assert _formal_09() == 3
    health = client.get("/api/health").json()
    assert health["five_chain_aligned"] is True
    assert health["five_chain"]["breaches"] == []


def test_atomic_failed_batch_never_leaves_half_formal(client, monkeypatch):
    """夹具二：atomic 整批失败路径上被塞入半截正式行 → 重放发现后回滚，正式零新增。"""
    set_mode(client, "atomic")
    rows = [dict(EST_BATCH[0]),
            {"meter_id": "M-GHOST", "period_id": "2026-09", "qty": 1.0, "doc_no": "F-BAD"}]
    before_formal = count(None, "SELECT COUNT(*) FROM estimates")
    before_receipts = _receipt_rows()

    real_write_receipt = loader_mod._write_receipt

    def poisoned_receipt(conn, **kwargs):
        real_write_receipt(conn, **kwargs)
        if kwargs["status"] == "failed":
            # 模拟"整批失败已半截"：回执落库后偷偷塞一行正式读数，盖本章 receipt_id
            _legacy_insert(conn, kwargs["kind"], [rows[0]], kwargs["receipt_id"])

    monkeypatch.setattr(loader_mod, "_write_receipt", poisoned_receipt)

    r = client.post("/api/estimates/batch", json={"period_id": "2026-09", "rows": rows})
    assert r.status_code == 500
    assert "重放" in r.json()["detail"]
    # 半截正式行与失败回执随事务一并回滚
    assert count(None, "SELECT COUNT(*) FROM estimates") == before_formal
    assert _formal_09() == 0
    assert _receipt_rows() == before_receipts
    assert _quarantine() == 0


def _legacy_insert(conn, kind, rows, receipt_id):
    """绕过装载正常路径的正式表写入，用于在失败路径上制造半截行。"""
    import uuid
    ts = loader_mod._now()
    for row in rows:
        rid = "E-" + uuid.uuid4().hex[:10]
        conn.execute(
            "INSERT INTO estimates(estimate_id,meter_id,period_id,qty,doc_no,loaded_at,receipt_id)"
            " VALUES (?,?,?,?,?,?,?)",
            (rid, row["meter_id"], row["period_id"], float(row["qty"]),
             row["doc_no"], ts, receipt_id),
        )


def test_quarantine_over_report_rolls_back(client, monkeypatch):
    """夹具三：split 下隔离区比回执失败行多写一条 → 重放发现隔离多报，整批回滚。"""
    set_mode(client, "split")
    rows = [dict(EST_BATCH[0]),
            {"meter_id": "M-GHOST", "period_id": "2026-09", "qty": 1.0, "doc_no": "Q-BAD"}]
    before_formal = count(None, "SELECT COUNT(*) FROM estimates")
    before_q = _quarantine()
    before_receipts = _receipt_rows()

    real_q = loader_mod._write_quarantine

    def over_report(conn, **kwargs):
        n = real_q(conn, **kwargs)
        # 再塞一条不属于本批失败集合的隔离行
        extra = dict(kwargs["failures"][0])
        extra["row_index"] = 999
        return n + real_q(conn, receipt_id=kwargs["receipt_id"], kind=kwargs["kind"],
                          period_id=kwargs["period_id"], failures=[extra])

    monkeypatch.setattr(loader_mod, "_write_quarantine", over_report)

    r = client.post("/api/estimates/batch", json={"period_id": "2026-09", "rows": rows})
    assert r.status_code == 500
    assert "隔离" in r.json()["detail"]
    # 正式半截与隔离多报全部回滚
    assert count(None, "SELECT COUNT(*) FROM estimates") == before_formal
    assert _quarantine() == before_q
    assert _receipt_rows() == before_receipts


def test_half_loaded_rows_cannot_enter_align(client, monkeypatch):
    """夹具四：装载与对齐交错时，半截正式行已回滚，对齐只吃可解释（带回执）的正式行。"""
    set_mode(client, "split")
    before_formal = count(None, "SELECT COUNT(*) FROM estimates")

    real_insert = loader_mod._insert_formal

    def dropping_insert(conn, kind, rows, receipt_id):
        return real_insert(conn, kind, rows[:-1], receipt_id)

    monkeypatch.setattr(loader_mod, "_insert_formal", dropping_insert)
    bad = client.post("/api/estimates/batch",
                      json={"period_id": "2026-09", "rows": EST_BATCH})
    assert bad.status_code == 500
    monkeypatch.undo()

    # 回滚后该账期无正式行，对齐无输入可吃
    r = client.post("/api/align", json={"period_id": "2026-09"})
    assert r.status_code == 400
    assert "无估抄/实抄可对齐" in r.json()["detail"]

    # 正常装载后再对齐：每个正式行都带 receipt_id，可由回执解释
    ok = client.post("/api/estimates/batch",
                     json={"period_id": "2026-09", "rows": EST_BATCH}).json()
    assert ok["success_count"] == 3
    orphans = count(
        None,
        "SELECT COUNT(*) FROM estimates WHERE period_id='2026-09' AND receipt_id IS NULL")
    assert orphans == 0
    client.post("/api/actuals/batch", json={
        "period_id": "2026-09",
        "rows": [{"meter_id": "M-W-101", "period_id": "2026-09", "qty": 100.0,
                  "occurred_at": "2026-09-28T10:00:00", "doc_no": "FA-1"}],
    })
    align = client.post("/api/align", json={"period_id": "2026-09"}).json()
    # 3 个表具：M-W-101 匹配，另两个只有估抄 → variance
    assert align["matched_count"] == 1
    assert align["variance_count"] == 2
    assert count(None, "SELECT COUNT(*) FROM estimates") == before_formal + 3


def test_concurrent_batches_each_self_consistent(client):
    """夹具五：并发两批，一批全合法、一批夹坏行；写事务串行化，各自闭合互不交叉污染。"""
    set_mode(client, "split")
    good_batch = [
        {"meter_id": f"M-W-10{(i % 2) + 1}", "period_id": "2026-09",
         "qty": 10 + i, "doc_no": f"CC-OK-{i}"}
        for i in range(4)
    ]
    # 坏批：前 3 行合法、第 4 行表具不存在。若事务不原子就会留 3 行半截
    bad_batch = [
        {"meter_id": f"M-E-20{(i % 2) + 1}", "period_id": "2026-09",
         "qty": 20 + i, "doc_no": f"CC-BAD-{i}"}
        for i in range(4)
    ]
    bad_batch[3]["meter_id"] = "M-GHOST"
    results: list = []

    from fastapi.testclient import TestClient
    from app.main import app

    def post(rows, tag):
        c = TestClient(app)
        r = c.post("/api/estimates/batch",
                   json={"period_id": "2026-09", "rows": rows})
        results.append((tag, r.status_code, r.json()))

    t1 = threading.Thread(target=post, args=(good_batch, "ok"))
    t2 = threading.Thread(target=post, args=(bad_batch, "bad"))
    t1.start(); t2.start(); t1.join(); t2.join()

    by_tag = {tag: (code, body) for tag, code, body in results}
    ok_code, ok_body = by_tag["ok"]
    bad_code, bad_body = by_tag["bad"]
    assert ok_code == 200 and ok_body["status"] == "success"
    assert bad_code == 200 and bad_body["status"] == "partial"

    # 好批 4 行整批可见；坏批恰好 3 行正式 + 1 行隔离，不存在串号或半截
    ok_rows = count(
        None,
        "SELECT COUNT(*) FROM estimates WHERE period_id='2026-09' AND doc_no LIKE 'CC-OK-%'")
    bad_formal = count(
        None,
        "SELECT COUNT(*) FROM estimates WHERE period_id='2026-09' AND doc_no LIKE 'CC-BAD-%'")
    assert ok_rows == 4 and bad_formal == 3
    assert _quarantine() == 1
    # 两批的正式行各挂各的 receipt_id，无交叉
    cross = count(
        None,
        "SELECT COUNT(*) FROM estimates WHERE period_id='2026-09' AND ("
        f"(doc_no LIKE 'CC-OK-%' AND receipt_id!='{ok_body['receipt_id']}') OR "
        f"(doc_no LIKE 'CC-BAD-%' AND receipt_id!='{bad_body['receipt_id']}'))")
    assert cross == 0
    assert client.get("/api/health").json()["five_chain_aligned"] is True


def test_overview_counts_follow_database_not_receipt_claims(client):
    """总览装载计数跟落库：正常装载后正式/隔离/回执三方与 health 重放一致。"""
    set_mode(client, "split")
    rows = [
        {"meter_id": "M-W-101", "period_id": "2026-09", "qty": 1, "doc_no": "OV-1"},
        {"meter_id": "M-W-102", "period_id": "2026-09", "qty": 2, "doc_no": "OV-2"},
        {"meter_id": "M-GONE", "period_id": "2026-09", "qty": 3, "doc_no": "OV-BAD"},
    ]
    rcpt = client.post("/api/estimates/batch",
                       json={"period_id": "2026-09", "rows": rows}).json()
    assert rcpt["success_count"] == 2 and rcpt["fail_count"] == 1

    ov = client.get("/api/overview").json()
    formal_09 = count(None, "SELECT COUNT(*) FROM estimates WHERE period_id='2026-09'")
    assert formal_09 == 2
    assert ov["quarantine_count"] == 1
    # 总览的回执累计与正式表累计同源同值
    receipts = client.get("/api/receipts?period_id=2026-09").json()
    assert sum(r["success_count"] for r in receipts) == formal_09
    assert sum(r["fail_count"] for r in receipts
               if r["load_mode"] == "split") == ov["quarantine_count"]
    assert ov["five_chain_aligned"] is True
    assert ov["five_chain_breaches"] == []
    # 污染字段已彻底移除
    assert "bulk_success_painted" not in ov


def test_orphan_formal_rows_blocked_from_align(client):
    """无 receipt_id 的孤儿正式行不可解释：对齐入口拒绝，禁止半截数据进对账批。"""
    set_mode(client, "split")
    # 正常装载一行可解释正式行
    client.post("/api/estimates/batch", json={
        "period_id": "2026-09",
        "rows": [{"meter_id": "M-W-101", "period_id": "2026-09",
                  "qty": 7.0, "doc_no": "ORPH-OK"}],
    })
    # 绕过装载服务直接塞一行无溯源章的孤儿行（模拟五链撕裂）
    import sqlite3
    import app.db as db_mod
    raw = sqlite3.connect(db_mod.DB_PATH)
    try:
        raw.execute(
            "INSERT INTO estimates(estimate_id,meter_id,period_id,qty,doc_no,loaded_at,receipt_id)"
            " VALUES ('E-ORPH','M-W-102','2026-09',8.0,'ORPH-GHOST','t',NULL)")
        raw.commit()
    finally:
        raw.close()

    r = client.post("/api/align", json={"period_id": "2026-09"})
    assert r.status_code == 409
    assert "溯源" in r.json()["detail"]
    # 没有产出任何对账批
    assert count(None, "SELECT COUNT(*) FROM reconcile_batches WHERE period_id='2026-09'") == 0


def test_failed_receipt_keys_never_present_in_formal(client):
    """两种架构分别核对：回执失败行的键绝不出现在正式表；成功键与正式键并集一致。"""
    for mode in ("atomic", "split"):
        set_mode(client, mode)
        rows = [
            {"meter_id": "M-W-101", "period_id": "2026-09", "qty": 1, "doc_no": f"K-{mode}-OK"},
            {"meter_id": "M-GHOST", "period_id": "2026-09", "qty": 1, "doc_no": f"K-{mode}-BAD"},
        ]
        rcpt = client.post("/api/estimates/batch",
                           json={"period_id": "2026-09", "rows": rows}).json()
        bad_key_present = count(
            None,
            f"SELECT COUNT(*) FROM estimates WHERE period_id='2026-09' "
            f"AND doc_no='K-{mode}-BAD'")
        assert bad_key_present == 0
        if mode == "atomic":
            assert rcpt["status"] == "failed" and rcpt["success_count"] == 0
            ok_key_present = count(
                None,
                f"SELECT COUNT(*) FROM estimates WHERE doc_no='K-{mode}-OK'")
            assert ok_key_present == 0
        else:
            assert rcpt["status"] == "partial" and rcpt["success_count"] == 1
            ok_key_present = count(
                None,
                f"SELECT COUNT(*) FROM estimates WHERE doc_no='K-{mode}-OK'")
            assert ok_key_present == 1
    assert client.get("/api/health").json()["five_chain_aligned"] is True
