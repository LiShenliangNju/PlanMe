"""仪表盘「近期动态」聚合接口。

从 homework_items、lecture_notes 两张表中聚合最近 N 天的事件，按时间倒序返回，
供 Web 首页「近期动态」卡片动态渲染。
"""
import time
from typing import Any

import aiosqlite
from fastapi import APIRouter, Query

from core.homework.message_store import resolve_db_path

router = APIRouter(prefix="/api/activity", tags=["Activity"])


def _fmt_time(ts: int) -> str:
    """把 Unix 时间戳格式化为易读字符串（今天/昨天/MM-DD）。"""
    if not ts:
        return ""
    now = time.localtime()
    today = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
    t = time.localtime(ts)
    if ts >= today:
        return time.strftime("%H:%M", t)
    if ts >= today - 86400:
        return "昨天"
    return time.strftime("%m-%d", t)


def _build_homework_event(row: dict[str, Any]) -> dict[str, Any] | None:
    """根据 homework_items 行构造活动描述。"""
    subject = row.get("subject") or ""
    raw = row.get("raw_content") or ""
    if not subject:
        # 模型未抽出标题时，用原始消息前 25 字兜底
        raw = raw.replace('\n', ' ').replace('\r', ' ').strip()
        subject = (raw[:25] + "…") if len(raw) > 25 else raw
    if not subject:
        subject = "群消息"
    deadline = row.get("deadline") or ""
    status = row.get("status") or ""
    group = row.get("group_name") or row.get("group_id") or ""

    # 优先用 decided_at（确认/忽略时间）作为活动时间，其次 created_at
    ts = row.get("decided_at") or row.get("created_at") or 0
    if not ts:
        return None

    if status == "pending":
        text = f"🤖 识别到作业《{subject}》待确认" + (f"（{group}）" if group else "")
    elif status in ("confirmed", "auto"):
        text = f"📚 作业《{subject}》已确认" + (f"，截止 {deadline}" if deadline else "")
    elif status == "ignored":
        text = f"🗑️ 作业《{subject}》已被忽略"
    elif status == "drop":
        text = f"⛔ 作业《{subject}》判定为非作业"
    else:
        text = f"📝 作业《{subject}" + (f" · 状态 {status}" if status else "")
    return {"time": ts, "text": text, "formatted": _fmt_time(ts)}


def _build_lecture_event(row: dict[str, Any]) -> dict[str, Any] | None:
    """根据 lecture_notes 行构造活动描述。"""
    group = row.get("group_name") or row.get("group_id") or ""
    status = row.get("status") or ""

    # OCR 完成时间优先，其次图片落库时间
    ts = row.get("ocr_at") or row.get("created_at") or 0
    if not ts:
        return None

    if status == "pending":
        text = f"🖼️ 收到图片通知，排队等待 OCR" + (f"（{group}）" if group else "")
    elif status in ("active", "done"):
        text = f"✅ 图片通知 OCR 完成并已存档" + (f"（{group}）" if group else "")
    else:
        text = f"⚠️ 图片通知 OCR 异常（{status}）" + (f"（{group}）" if group else "")
    return {"time": ts, "text": text, "formatted": _fmt_time(ts)}


@router.get("")
async def recent_activity(days: int = Query(3, ge=1, le=30)):
    """返回最近 days 天的系统活动动态。"""
    path = resolve_db_path()
    cutoff = int(time.time()) - days * 86400
    activities: list[dict[str, Any]] = []

    try:
        db = await aiosqlite.connect(path)
        await db.execute("PRAGMA journal_mode=WAL")

        # homework 事件
        cur = await db.execute(
            "SELECT subject, deadline, status, raw_content, created_at, decided_at, group_name, group_id "
            "FROM homework_items WHERE created_at >= ? OR decided_at >= ?",
            (cutoff, cutoff),
        )
        cols = [d[0] for d in cur.description]
        async for row in cur:
            item = dict(zip(cols, row))
            ev = _build_homework_event(item)
            if ev:
                activities.append(ev)

        # lecture 事件
        cur2 = await db.execute(
            "SELECT group_name, group_id, status, created_at, ocr_at "
            "FROM lecture_notes WHERE created_at >= ? OR ocr_at >= ?",
            (cutoff, cutoff),
        )
        cols2 = [d[0] for d in cur2.description]
        async for row in cur2:
            item = dict(zip(cols2, row))
            ev = _build_lecture_event(item)
            if ev:
                activities.append(ev)

        await db.close()
    except Exception as exc:  # noqa: BLE001
        return {"activities": [], "error": str(exc)}

    activities.sort(key=lambda x: x["time"], reverse=True)
    return {"activities": activities}
