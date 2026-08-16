"""SQLite 消息存储：增量去重，避免重复处理/重复询问。

设计：
- messages 表以 message_id 为主键，INSERT OR IGNORE 天然去重（含重启后）。
- 记录每个群已处理到的最后时间戳 last_seq 表，便于日志排查与未来可能的历史拉取。
"""

import logging
from typing import Optional
import aiosqlite

from schemas.homework_schema import GroupMessage, HomeworkItem, LectureNote

logger = logging.getLogger("store")


def resolve_db_path() -> str:
    """解析扫描器使用的 db 路径（与 scanner 完全一致：相对路径以项目根为基准）。"""
    from settings import settings
    import yaml
    from pathlib import Path

    cfg_path = Path(settings.HMWK_SCRN_CONFIG_PATH)
    db_path = "qq_homework.db"
    try:
        with cfg_path.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        db_path = (cfg.get("storage") or {}).get("db_path", db_path)
    except Exception:  # noqa: BLE001
        pass
    p = Path(db_path)
    if not p.is_absolute():
        p = Path(settings.BASE_DIR) / db_path
    return str(p)


class MessageStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        # WAL：允许 API 进程并发读，而扫描器持续写，互不阻塞
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                message_id INTEGER PRIMARY KEY,
                group_id   INTEGER,
                user_id    INTEGER,
                role       TEXT,
                content    TEXT,
                time       INTEGER
            )
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS group_progress (
                group_id   INTEGER PRIMARY KEY,
                last_time  INTEGER
            )
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS homework_items (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id   INTEGER UNIQUE,
                cid          TEXT,
                group_id     INTEGER,
                group_name   TEXT,
                user_id      INTEGER,
                is_homework  INTEGER,
                subject      TEXT,
                deadline     TEXT,
                description  TEXT,
                confidence   REAL,
                status       TEXT,
                raw_content  TEXT,
                created_at   INTEGER,
                decided_at   INTEGER
            )
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS lecture_notes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id  INTEGER NOT NULL,
                image_seq   INTEGER NOT NULL DEFAULT 0,
                group_id    INTEGER,
                group_name  TEXT,
                user_id     INTEGER,
                image_url   TEXT,
                ocr_md      TEXT,
                status      TEXT,
                created_at  INTEGER,
                ocr_at      INTEGER,
                UNIQUE(message_id, image_seq)
            )
            """
        )
        await self._db.commit()
        logger.info("SQLite 初始化完成：%s", self.db_path)

    async def save(self, msg: GroupMessage) -> bool:
        """保存消息，返回 True 表示是首次（未处理过）。"""
        assert self._db is not None
        try:
            before = self._db.total_changes
            await self._db.execute(
                "INSERT OR IGNORE INTO messages "
                "(message_id, group_id, user_id, role, content, time) VALUES (?,?,?,?,?,?)",
                (
                    msg.message_id,
                    msg.group_id,
                    msg.sender.user_id,
                    msg.sender.role,
                    msg.content,
                    msg.time,
                ),
            )
            inserted = self._db.total_changes - before
            await self._db.execute(
                "INSERT OR REPLACE INTO group_progress (group_id, last_time) VALUES (?,?)",
                (msg.group_id, msg.time),
            )
            await self._db.commit()
            # inserted>0 表示消息是首次落库（实现跨重启去重）
            return inserted > 0
        except Exception as exc:
            logger.exception("保存消息失败：%s", exc)
            return False

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()

    # ------------------------------------------------------------------
    # homework_items：作业识别结果 + 决策状态（Web 展示 / 历史回看权威来源）
    # ------------------------------------------------------------------
    async def save_homework_item(self, item: HomeworkItem) -> bool:
        """插入一条作业记录（message_id 唯一，重启后天然去重）。返回是否首次插入。"""
        assert self._db is not None
        try:
            before = self._db.total_changes
            await self._db.execute(
                """
                INSERT OR IGNORE INTO homework_items
                (message_id, cid, group_id, group_name, user_id, is_homework,
                 subject, deadline, description, confidence, status, raw_content,
                 created_at, decided_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    item.message_id, item.cid, item.group_id, item.group_name,
                    item.user_id, int(item.is_homework), item.subject, item.deadline,
                    item.description, item.confidence, item.status, item.raw_content,
                    item.created_at, item.decided_at,
                ),
            )
            inserted = self._db.total_changes - before
            await self._db.commit()
            return inserted > 0
        except Exception as exc:
            logger.exception("保存作业记录失败：%s", exc)
            return False

    async def update_homework_status(
        self,
        message_id: int,
        status: str,
        decided_at: int,
        subject: Optional[str] = None,
        deadline: Optional[str] = None,
        description: Optional[str] = None,
    ) -> None:
        """更新某条作业的决策状态（确认 / 忽略 / 自动加入等）。"""
        assert self._db is not None
        try:
            sql = "UPDATE homework_items SET status=?, decided_at=?"
            params: list = [status, decided_at]
            if subject is not None:
                sql += ", subject=?"
                params.append(subject)
            if deadline is not None:
                sql += ", deadline=?"
                params.append(deadline)
            if description is not None:
                sql += ", description=?"
                params.append(description)
            sql += " WHERE message_id=?"
            params.append(message_id)
            await self._db.execute(sql, tuple(params))
            await self._db.commit()
        except Exception as exc:
            logger.exception("更新作业状态失败 #%s：%s", message_id, exc)

    async def recent_homework_items(
        self, limit: int = 100, statuses: Optional[list[str]] = None
    ) -> list[dict]:
        """读取作业记录（默认按抓取时间倒序），供 Web 展示。"""
        assert self._db is not None
        sql = (
            "SELECT message_id, cid, group_id, group_name, user_id, is_homework, "
            "subject, deadline, description, confidence, status, raw_content, "
            "created_at, decided_at FROM homework_items"
        )
        params: list = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" WHERE status IN ({placeholders})"
            params = list(statuses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        try:
            cur = await self._db.execute(sql, tuple(params))
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]
        except Exception as exc:
            logger.exception("读取作业记录失败：%s", exc)
            return []

    async def pending_homework_items(self) -> list[dict]:
        """取出仍为 pending 的作业（用于进程重启后恢复 notifier 内存态）。"""
        return await self.recent_homework_items(limit=200, statuses=["pending"])

    # ------------------------------------------------------------------
    # lecture_notes：白名单群图片 OCR 后的 Markdown 笔记
    # ------------------------------------------------------------------
    async def save_lecture_note(self, note: LectureNote) -> bool:
        """插入一条讲座笔记（message_id+image_seq 唯一，去重）。返回是否首次插入。"""
        assert self._db is not None
        try:
            before = self._db.total_changes
            await self._db.execute(
                """
                INSERT OR IGNORE INTO lecture_notes
                (message_id, image_seq, group_id, group_name, user_id, image_url,
                 ocr_md, status, created_at, ocr_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    note.message_id, note.image_seq, note.group_id, note.group_name,
                    note.user_id, note.image_url, note.ocr_md, note.status,
                    note.created_at, note.ocr_at,
                ),
            )
            inserted = self._db.total_changes - before
            await self._db.commit()
            return inserted > 0
        except Exception as exc:
            logger.exception("保存讲座笔记失败：%s", exc)
            return False

    async def recent_lecture_notes(self, limit: int = 100) -> list[dict]:
        """按抓取时间倒序读取讲座笔记，供 Web 展示。"""
        assert self._db is not None
        sql = (
            "SELECT id, message_id, image_seq, group_id, group_name, user_id, "
            "image_url, ocr_md, status, created_at, ocr_at FROM lecture_notes "
            "ORDER BY created_at DESC LIMIT ?"
        )
        try:
            cur = await self._db.execute(sql, (limit,))
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]
        except Exception as exc:
            logger.exception("读取讲座笔记失败：%s", exc)
            return []
