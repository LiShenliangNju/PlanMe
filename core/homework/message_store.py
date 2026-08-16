"""SQLite 消息存储：增量去重 + 断点续抓 + OCR 任务队列。

设计：
- messages 表以 message_id 为主键，INSERT OR IGNORE 天然去重（含重启后）。
- group_progress 记录每个群「已处理到哪」（last_time + last_message_id），
  进程启动时据此调 OneBot get_group_msg_history 补抓空窗期消息
  （push 模式下 NapCat 不重放历史，不补抓就是永久丢失）。
- lecture_notes 兼任「OCR 任务队列」：图片一到先落盘 + 落库 status='pending'，
  OCR 由后台 worker 慢慢消费并回填，因此杀进程 / 重启都不丢图。
"""

import logging
import time
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
                group_id        INTEGER PRIMARY KEY,
                last_time       INTEGER,
                last_message_id INTEGER,
                updated_at      INTEGER
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
                local_path  TEXT,
                ocr_md      TEXT,
                status      TEXT,
                attempts    INTEGER DEFAULT 0,
                error       TEXT,
                created_at  INTEGER,
                ocr_at      INTEGER,
                UNIQUE(message_id, image_seq)
            )
            """
        )
        # 老库补列（幂等）：老版本建的表缺 local_path / last_message_id 等，
        # 没有这些列就无法「重启续跑 OCR」和「历史补抓」。
        await self._migrate()
        await self._db.commit()
        logger.info("SQLite 初始化完成：%s", self.db_path)

    async def _migrate(self) -> None:
        """为已存在的老表补齐新增列（ALTER TABLE ADD COLUMN，幂等且不丢数据）。"""
        await self._ensure_columns(
            "group_progress",
            {"last_message_id": "INTEGER", "updated_at": "INTEGER"},
        )
        await self._ensure_columns(
            "lecture_notes",
            {"local_path": "TEXT", "attempts": "INTEGER DEFAULT 0", "error": "TEXT"},
        )

    async def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        assert self._db is not None
        try:
            cur = await self._db.execute(f"PRAGMA table_info({table})")
            rows = await cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取表结构失败 %s：%s", table, exc)
            return
        existing = {r[1] for r in rows}
        if not existing:
            return  # 表不存在（正常情况下上面 CREATE 已建好，这里只是兜底）
        for name, ddl in columns.items():
            if name in existing:
                continue
            try:
                await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                logger.info("迁移：%s 表新增列 %s %s", table, name, ddl)
            except Exception as exc:  # noqa: BLE001
                logger.warning("迁移失败 %s.%s：%s", table, name, exc)

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
    # group_progress：断点锚点。每条群消息处理完都推进，重启后据此补抓历史。
    # 注意必须「单调递增」写入：补抓时会重放较老的消息，不能把锚点写回去。
    # ------------------------------------------------------------------
    async def mark_progress(self, group_id: int, message_id: int, msg_time: int) -> None:
        """推进某个群的处理锚点（只在更新的消息上前进，不回退）。"""
        assert self._db is not None
        try:
            await self._db.execute(
                """
                INSERT INTO group_progress (group_id, last_time, last_message_id, updated_at)
                VALUES (?,?,?,?)
                ON CONFLICT(group_id) DO UPDATE SET
                    last_time       = MAX(COALESCE(group_progress.last_time, 0), excluded.last_time),
                    last_message_id = CASE
                        WHEN excluded.last_time >= COALESCE(group_progress.last_time, 0)
                        THEN excluded.last_message_id
                        ELSE group_progress.last_message_id END,
                    updated_at      = excluded.updated_at
                """,
                (int(group_id), int(msg_time or 0), int(message_id or 0), int(time.time())),
            )
            await self._db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("推进群进度失败 #%s：%s", group_id, exc)

    async def get_progress(self, group_id: int) -> Optional[dict]:
        """取某个群的断点；从未处理过则返回 None。"""
        assert self._db is not None
        try:
            cur = await self._db.execute(
                "SELECT group_id, last_time, last_message_id, updated_at "
                "FROM group_progress WHERE group_id=?",
                (int(group_id),),
            )
            row = await cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取群进度失败 #%s：%s", group_id, exc)
            return None

    async def all_progress(self) -> list[dict]:
        """列出所有已有断点的群（补抓时与白名单取并集）。"""
        assert self._db is not None
        try:
            cur = await self._db.execute(
                "SELECT group_id, last_time, last_message_id, updated_at FROM group_progress"
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取全部群进度失败：%s", exc)
            return []

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
        """插入一条讲座笔记（message_id+image_seq 唯一，去重）。返回是否首次插入。

        「先落库后 OCR」下的主要用法：图片落盘后立刻以 status='pending' 入库，
        ocr_md 留空，由 OCR worker 之后回填。返回 False 表示这张图已经入过库
        （重复推送 / 补抓重放），直接跳过即可。
        """
        assert self._db is not None
        try:
            before = self._db.total_changes
            await self._db.execute(
                """
                INSERT OR IGNORE INTO lecture_notes
                (message_id, image_seq, group_id, group_name, user_id, image_url,
                 local_path, ocr_md, status, attempts, error, created_at, ocr_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    note.message_id, note.image_seq, note.group_id, note.group_name,
                    note.user_id, note.image_url, note.local_path, note.ocr_md,
                    note.status, note.attempts, note.error,
                    note.created_at, note.ocr_at,
                ),
            )
            inserted = self._db.total_changes - before
            await self._db.commit()
            return inserted > 0
        except Exception as exc:
            logger.exception("保存讲座笔记失败：%s", exc)
            return False

    async def pending_lecture_notes(self, limit: int = 500) -> list[dict]:
        """取出所有待 OCR 的图片任务（按抓取时间正序，先到先做）。

        进程启动时调用即实现「重启续跑」：上次被 taskkill 打断、
        或 Ollama 挂掉导致没跑完的图片，都会在这里被重新捞出来。
        """
        assert self._db is not None
        try:
            cur = await self._db.execute(
                "SELECT id, message_id, image_seq, group_id, group_name, user_id, "
                "image_url, local_path, status, attempts, created_at "
                "FROM lecture_notes WHERE status='pending' "
                "ORDER BY created_at ASC, image_seq ASC LIMIT ?",
                (limit,),
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]
        except Exception as exc:  # noqa: BLE001
            logger.exception("读取待 OCR 任务失败：%s", exc)
            return []

    async def count_pending_lecture_notes(self) -> int:
        """待 OCR 队列长度（供日志 / Web 显示积压情况）。"""
        assert self._db is not None
        try:
            cur = await self._db.execute(
                "SELECT COUNT(*) FROM lecture_notes WHERE status='pending'"
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0
        except Exception:  # noqa: BLE001
            return 0

    async def mark_lecture_ocr_done(
        self, message_id: int, image_seq: int, ocr_md: str
    ) -> None:
        """OCR 成功：回填 Markdown，status → active。"""
        assert self._db is not None
        try:
            await self._db.execute(
                "UPDATE lecture_notes SET ocr_md=?, status='active', error='', "
                "attempts=COALESCE(attempts,0)+1, ocr_at=? "
                "WHERE message_id=? AND image_seq=?",
                (ocr_md, int(time.time()), int(message_id), int(image_seq)),
            )
            await self._db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("回填 OCR 结果失败 #%s/%s：%s", message_id, image_seq, exc)

    async def mark_lecture_ocr_failed(
        self, message_id: int, image_seq: int, error: str, give_up: bool
    ) -> None:
        """OCR 失败：累加 attempts。give_up=True 时置 error（不再重试），否则留在 pending。"""
        assert self._db is not None
        status = "error" if give_up else "pending"
        try:
            await self._db.execute(
                "UPDATE lecture_notes SET status=?, error=?, "
                "attempts=COALESCE(attempts,0)+1, ocr_at=? "
                "WHERE message_id=? AND image_seq=?",
                (status, error[:500], int(time.time()), int(message_id), int(image_seq)),
            )
            await self._db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("记录 OCR 失败状态出错 #%s/%s：%s", message_id, image_seq, exc)

    async def update_lecture_local_path(
        self, message_id: int, image_seq: int, local_path: str
    ) -> None:
        """图片被重新下载后更新本地路径（原缓存被 NapCat 清理时的兜底）。"""
        assert self._db is not None
        try:
            await self._db.execute(
                "UPDATE lecture_notes SET local_path=? WHERE message_id=? AND image_seq=?",
                (local_path, int(message_id), int(image_seq)),
            )
            await self._db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("更新图片本地路径失败 #%s/%s：%s", message_id, image_seq, exc)

    async def recent_lecture_notes(self, limit: int = 100) -> list[dict]:
        """按抓取时间倒序读取讲座笔记，供 Web 展示。"""
        assert self._db is not None
        sql = (
            "SELECT id, message_id, image_seq, group_id, group_name, user_id, "
            "image_url, local_path, ocr_md, status, attempts, error, created_at, ocr_at "
            "FROM lecture_notes ORDER BY created_at DESC LIMIT ?"
        )
        try:
            cur = await self._db.execute(sql, (limit,))
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]
        except Exception as exc:
            logger.exception("读取讲座笔记失败：%s", exc)
            return []
