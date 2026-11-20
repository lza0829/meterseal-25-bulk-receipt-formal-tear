import os
import shutil
import tempfile

import pytest

# 必须在导入应用代码之前指向临时库，保证测试不碰真实 data/meterseal.db
_TMP = tempfile.mkdtemp(prefix="meterseal-test-")
os.environ["METALSEAL_TEST_DIR"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402

import app.db as db_mod  # noqa: E402
from app import seed  # noqa: E402
from app.main import app  # noqa: E402

# 全局只种子一次到模板库；每个测试复制模板，省去重复建表/种子开销
_TEMPLATE = os.path.join(_TMP, "template.db")
db_mod.DB_PATH = _TEMPLATE
seed.seed_if_empty()


@pytest.fixture()
def client():
    # 每个测试用一个全新库文件（模板的拷贝），彻底隔离
    db_file = os.path.join(_TMP, f"test-{os.getpid()}-{id(object())}.db")
    shutil.copyfile(_TEMPLATE, db_file)
    db_mod.DB_PATH = db_file
    with TestClient(app) as c:
        yield c
    try:
        os.remove(db_file)
    except OSError:
        pass


def set_mode(client, mode: str):
    r = client.put("/api/settings/load_mode", json={"value": mode})
    assert r.status_code == 200, r.text


def count(_conn, sql):
    import sqlite3
    conn = sqlite3.connect(db_mod.DB_PATH)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


EST_BATCH = [
    {"meter_id": "M-W-101", "period_id": "2026-10", "qty": 100.0, "doc_no": "B-D1"},
    {"meter_id": "M-W-102", "period_id": "2026-10", "qty": 200.0, "doc_no": "B-D2"},
    # 坏行：表具不存在
    {"meter_id": "M-NOPE", "period_id": "2026-10", "qty": 300.0, "doc_no": "B-D3"},
]
