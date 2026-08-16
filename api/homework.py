"""作业扫描器相关 HTTP 接口（webapp napcat 窗口的数据源）。

提供两类数据：
- 内存态：pending 确认项（notifier 运行时）、feed 事件总线（qqbot 推送流）。
- db 态：/items 直接读 homework_items 表，是「作业列表 / 历史回看」的权威来源，
         重启不丢、可筛选状态。

注意：本模块只读展示，确认动作仍在 QQ 私聊中完成（与主流程一致）。
"""
import aiosqlite
from fastapi import APIRouter

from app.services import services
from core.homework.message_store import resolve_db_path

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


@router.get("/items")
async def homework_items(limit: int = 200):
    """直接读 db 的 homework_items 表：作业识别结果 + 决策状态（权威列表）。

    与 /pending（notifier 内存态）互补：这里可历史回看、重启不丢、按状态筛选。
    """
    path = resolve_db_path()
    try:
        db = await aiosqlite.connect(path)
        await db.execute("PRAGMA journal_mode=WAL")
        cur = await db.execute(
            "SELECT message_id, cid, group_id, group_name, user_id, is_homework, "
            "subject, deadline, description, confidence, status, raw_content, "
            "created_at, decided_at FROM homework_items ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        await db.close()
        return {"items": [dict(zip(cols, r)) for r in rows]}
    except Exception as exc:
        return {"items": [], "error": str(exc)}
