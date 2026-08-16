"""讲座/通知图片 OCR 存档的读取接口。

白名单群里的图片经扫描器 OCR 后存入 lecture_notes 表，本接口直接读 db 返回，
供 Web「🖼️ 讲座/通知」Tab 展示。Web 只读取、不修改。
"""
import aiosqlite
from fastapi import APIRouter

from core.homework.message_store import resolve_db_path

router = APIRouter(prefix="/api/lecture", tags=["Lecture OCR"])


@router.get("/notes")
async def lecture_notes(limit: int = 100):
    """按抓取时间倒序返回讲座/通知笔记（含 OCR 得到的 Markdown）。"""
    path = resolve_db_path()
    try:
        db = await aiosqlite.connect(path)
        await db.execute("PRAGMA journal_mode=WAL")
        cur = await db.execute(
            "SELECT id, message_id, image_seq, group_id, group_name, user_id, "
            "image_url, local_path, ocr_md, status, attempts, error, created_at, ocr_at "
            "FROM lecture_notes ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        notes = [dict(zip(cols, r)) for r in rows]
        # 队列积压：pending 表示已落库存档、OCR 还在排队（本地模型慢时会有一批）
        cur2 = await db.execute(
            "SELECT COUNT(*) FROM lecture_notes WHERE status='pending'"
        )
        row2 = await cur2.fetchone()
        await db.close()
        return {"notes": notes, "pending": int(row2[0]) if row2 else 0}
    except Exception as exc:
        return {"notes": [], "pending": 0, "error": str(exc)}
