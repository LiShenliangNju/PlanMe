"""HomeworkScanner：QQ 群作业扫描服务（合并原 core/homework/__main__.py 入口逻辑）。

作为主程序的同进程后台任务运行：由 app 的 lifespan 调用 run() 拉起，
stop() 在 lifespan 关闭时调用。扫描器仍只把作业转成自然语言 POST 给主系统
/api/chat，不自己写 iCloud；其 qqbot 推送 / 建议日程通过 core.napcat.feed 暴露。
"""
import asyncio
import logging
import re
from pathlib import Path
import yaml

from settings import settings
from core.homework.detector import HomeworkDetector, ACTION_AUTO, ACTION_ASK, ACTION_DROP
from core.homework.message_store import MessageStore
from core.homework.notifier import Notifier
from core.homework.onebot_client import OneBotClient
from core.homework.scheduler_bridge import SchedulerBridge
from schemas.homework_schema import GroupMessage, Sender

logger = logging.getLogger("homework.scanner")

_CQ_RE = re.compile(r"\[CQ:[^\]]*\]")


class HomeworkScanner:
    def __init__(self, feed=None) -> None:
        self.feed = feed
        self.running = False
        # 运行时状态（run() 内初始化）
        self._client = None
        self._store = None
        self._notifier = None
        self._detector = None
        self._owner_id = None
        self._group_whitelist = set()
        self._teacher_ids = set()
        self._teacher_roles = set()

    @staticmethod
    def strip_cq(text: str) -> str:
        return _CQ_RE.sub("", text).strip()

    def load_config(self) -> dict:
        cfg_path = Path(settings.HMWK_SCRN_CONFIG_PATH)
        with cfg_path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    async def run(self) -> None:
        self.running = True
        cfg = self.load_config()
        qq_cfg = cfg["qq"]
        self._owner_id = int(qq_cfg["owner_user_id"])
        self._group_whitelist = set(qq_cfg.get("group_whitelist") or [])
        self._teacher_ids = set(int(x) for x in (qq_cfg.get("teacher_user_ids") or []))
        self._teacher_roles = set(qq_cfg.get("teacher_roles") or [])

        db_path = cfg["storage"]["db_path"]
        if not Path(db_path).is_absolute():
            db_path = str(Path(settings.BASE_DIR) / db_path)
        store = MessageStore(db_path)
        await store.init()
        self._store = store

        detector = HomeworkDetector(
            host=settings.OLLAMA_HOST,
            model=settings.OLLAMA_MODEL,
            temperature=settings.HMWK_DETECTOR_TEMPERATURE,
            keyword_prefilter=cfg["detector"]["keyword_prefilter"],
            throttle_seconds=cfg["detector"]["throttle_seconds"],
            min_confidence=cfg["detector"]["min_confidence"],
            auto_confidence=cfg["detector"].get("auto_confidence", 0.9),
        )
        self._detector = detector

        bridge = SchedulerBridge(cfg["scheduler"]["endpoint"], cfg["scheduler"]["timeout"])
        client = OneBotClient(
            ws_url=qq_cfg["onebot_ws_url"],
            access_token=qq_cfg["access_token"],
            on_event=self._on_event,
        )
        self._client = client
        notifier = Notifier(
            onebot=client,
            owner_id=self._owner_id,
            bridge=bridge,
            confirm_timeout=cfg["notifier"]["confirm_timeout_seconds"],
            feed=self.feed,
        )
        self._notifier = notifier

        if self.feed:
            self.feed.publish("status", "作业扫描器已启动，等待 QQ 群消息…")
        logger.info("作业扫描器启动，等待 QQ 群消息…")
        try:
            await client.run_forever()
        finally:
            self.running = False
            if self.feed:
                self.feed.publish("status", "作业扫描器已停止")

    async def _on_event(self, event: dict) -> None:
        if event.get("post_type") != "message":
            return
        mtype = event.get("message_type")
        if mtype == "group":
            await self._handle_group(event)
        elif mtype == "private":
            await self._notifier.handle_reply(
                event.get("user_id"), self.strip_cq(event.get("message", ""))
            )

    async def _handle_group(self, event: dict) -> None:
        group_id = int(event.get("group_id", 0))
        if self._group_whitelist and group_id not in self._group_whitelist:
            return
        sender_d = event.get("sender", {})
        role = sender_d.get("role", "member")
        user_id = int(sender_d.get("user_id", 0))
        if role not in self._teacher_roles and user_id not in self._teacher_ids:
            return

        content = self.strip_cq(event.get("message", ""))
        if not content:
            return

        msg = GroupMessage(
            message_id=int(event.get("message_id", 0)),
            group_id=group_id,
            sender=Sender(
                user_id=user_id,
                nickname=sender_d.get("nickname", ""),
                card=sender_d.get("card", ""),
                role=role,
            ),
            content=content,
            time=int(event.get("time", 0)),
        )

        if not await self._store.save(msg):
            return
        if not self._detector.prefilter(content):
            return

        group_name = event.get("group_name") or str(group_id)
        ex = await self._detector.detect(content, context=f"群：{group_name}")
        action = self._detector.decide_action(ex)
        if action == ACTION_AUTO:
            logger.info("高置信度作业，自动加入 #%s：%s | %s", group_id, ex.subject, ex.deadline)
            await self._notifier.auto_add(msg, ex, group_name)
        elif action == ACTION_ASK:
            logger.info("识别为作业，待确认 #%s：%s | %s", group_id, ex.subject, ex.deadline)
            await self._notifier.ask(msg, ex, group_name)
        else:
            logger.debug("低于置信度阈值，静默丢弃：%s | %s", ex.reason, content[:40])

    async def stop(self) -> None:
        self.running = False
        if self._client is not None:
            self._client.stop()
        if self._store is not None:
            try:
                await self._store.close()
            except Exception:  # noqa: BLE001
                pass
