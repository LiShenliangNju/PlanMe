"""SQLite 消息存储：增量去重，避免重复处理/重复询问。

设计：
- messages 表以 message_id 为主键，INSERT OR IGNORE 天然去重（含重启后）。
- 记录每个群已处理到的最后时间戳 last_seq 表，便于日志排查与未来可能的历史拉取。
"""

import logging
from typing import Optional
import aiosqlite

from schemas.homework_schema import GroupMessage

logger = logging.getLogger("store")


class MessageStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
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
