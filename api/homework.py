"""作业扫描器相关 HTTP 接口（webapp napcat 窗口的数据源）。

这些接口直接读取同进程内 HomeworkScanner 的内存状态（pending 确认项）与共享事件总线
feed（qqbot 推送 / 建议日程），从而把「qqbot 推送且建议添加的日程」呈现到 Web 界面。

注意：本模块只读展示，确认动作仍在 QQ 私聊中完成（与主流程一致）。
"""
from fastapi import APIRouter

from app.services import services

router = APIRouter(prefix="/api/homework", tags=["Homework Scanner"])


@router.get("/status")
async def scanner_status():
    s = services.homework
    if s is None:
        return {"enabled": False, "running": False, "detail": "扫描器未启用"}
    return {"enabled": True, "running": s.running, "owner_id": getattr(s, "_owner_id", None)}


@router.get("/pending")
async def pending_items():
    """列出当前等待主号确认的作业（notifier 内存状态）。"""
    s = services.homework
    if s is None or s._notifier is None:
        return {"pending": []}
    items = [
        {
            "cid": it.cid,
            "subject": it.extraction.subject,
            "deadline": it.extraction.deadline,
            "description": it.extraction.description,
            "group_name": it.group_name,
            "confidence": it.extraction.confidence,
        }
        for it in s._notifier.pending.values()
    ]
    return {"pending": items}


@router.get("/feed")
async def homework_feed(limit: int = 50):
    """返回最近的 qqbot 推送 / 建议日程事件。"""
    return {"feed": services.feed.recent(limit=limit)}
