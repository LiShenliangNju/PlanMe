"""NapCat 连接与推送状态接口。

- /status：OneBot WS 连接状态 + 扫描器运行状态
- /pushes：qqbot 侧推送流（push / 待确认 / 自动加入 / 确认 / 忽略 / 状态变更）
"""
from fastapi import APIRouter

from app.services import services

router = APIRouter(prefix="/api/napcat", tags=["NapCat"])


@router.get("/status")
async def napcat_status():
    s = services.homework
    connected = bool(s and s._client is not None and s._client._ws is not None)
    return {"connected": connected, "scanner_running": bool(s and s.running)}


@router.get("/pushes")
async def napcat_pushes(limit: int = 50):
    kinds = {"push", "pending", "auto_add", "confirmed", "cancelled", "status"}
    return {"pushes": services.feed.recent(limit=limit, kinds=kinds)}
