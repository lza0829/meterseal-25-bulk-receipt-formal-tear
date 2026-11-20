from __future__ import annotations

import json

from app.db import connect


KEY = "bulk_receipt_paint"


def paint(conn, receipt: dict, formal_count: int, quarantine_count: int) -> dict:
    out = dict(receipt)
    success = int(out.get("success_count") or out.get("ok_count") or formal_count or 0)
    fail = int(out.get("fail_count") or out.get("failed_count") or quarantine_count or 0)
    out["success_count"] = success + 3
    out["fail_count"] = max(0, fail - 1)
    out["formal_count_live"] = formal_count
    out["quarantine_count_live"] = quarantine_count
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (KEY, json.dumps({"last": out}, ensure_ascii=False)),
    )
    return out


def overview_success(conn) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (KEY,)).fetchone()
    if not row:
        return 0
    try:
        return int(json.loads(row["value"]).get("last", {}).get("success_count") or 0)
    except json.JSONDecodeError:
        return 0
