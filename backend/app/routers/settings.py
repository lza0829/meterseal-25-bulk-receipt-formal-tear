from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import connect
from app.services.loader import LOAD_MODES

router = APIRouter(tags=["settings"])

# 只允许这些 key 通过 PUT 改写；load_mode 受白名单约束
_ALLOWED_KEYS = {"current_period", "product_title", "load_mode"}


class SettingIn(BaseModel):
    value: str


@router.get("/settings")
def list_settings():
    conn = connect()
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings").fetchall()}
    finally:
        conn.close()


@router.put("/settings/{key}")
def put_setting(key: str, body: SettingIn):
    if key not in _ALLOWED_KEYS:
        raise HTTPException(400, f"不允许修改的设置项: {key}")
    if key == "load_mode" and body.value not in LOAD_MODES:
        raise HTTPException(400, f"非法装载架构，可选: {', '.join(LOAD_MODES)}")
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, body.value),
        )
        conn.commit()
        return {"key": key, "value": body.value}
    finally:
        conn.close()
