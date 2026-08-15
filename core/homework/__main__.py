"""入口：加载配置、连接 NapCat、路由群消息与私聊确认。

运行：
    pip install -r requirements.txt
    python -m core.homework
依赖 NapCat 已在本地运行（小号登录、仅绑 127.0.0.1、强 token），且 Ollama 已启动并拉取模型。
"""

import asyncio
import logging
import re
import sys
from pathlib import Path
import yaml

# 统一配置：注入 .config 到 sys.path
BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR / ".config"))
from settings import settings

from .detector import HomeworkDetector, ACTION_AUTO, ACTION_ASK, ACTION_DROP
from .message_store import MessageStore
from schemas.homework_schema import GroupMessage, Sender
from .notifier import Notifier
from .onebot_client import OneBotClient
from .scheduler_bridge import SchedulerBridge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("__name__")

CONFIG_PATH = Path(settings.HMWK_SCRN_CONFIG_PATH)


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


_CQ_RE = re.compile(r"\[CQ:[^\]]*\]") 
def strip_cq(text: str) -> str:
    return _CQ_RE.sub("", text).strip()


async def main() -> None:
    cfg = load_config()
    qq_cfg = cfg["qq"]
    owner_id = int(qq_cfg["owner_user_id"])
    group_whitelist = set(qq_cfg.get("group_whitelist") or [])
    teacher_ids = set(int(x) for x in (qq_cfg.get("teacher_user_ids") or []))
    teacher_roles = set(qq_cfg.get("teacher_roles") or [])

    db_path = cfg["storage"]["db_path"]
    if not Path(db_path).is_absolute():
        db_path = str(Path(settings.BASE_DIR) / db_path)
    store = MessageStore(db_path)
    await store.init()

    detector = HomeworkDetector(
        host=settings.OLLAMA_HOST,
        model=settings.OLLAMA_MODEL,
        temperature=settings.HMWK_DETECTOR_TEMPERATURE,
        keyword_prefilter=cfg["detector"]["keyword_prefilter"],
        throttle_seconds=cfg["detector"]["throttle_seconds"],
        min_confidence=cfg["detector"]["min_confidence"],
        auto_confidence=cfg["detector"].get("auto_confidence", 0.9),
    )

    async def handle_group(event: dict) -> None:
        group_id = int(event.get("group_id", 0))
        if group_whitelist and group_id not in group_whitelist:
            return

        sender_d = event.get("sender", {})
        role = sender_d.get("role", "member")
        user_id = int(sender_d.get("user_id", 0))
        if role not in teacher_roles and user_id not in teacher_ids:
            return

        content = strip_cq(event.get("message", ""))
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

        if not await store.save(msg):
            return
        if not detector.prefilter(content):
            return

        group_name = event.get("group_name") or str(group_id)
        ex = await detector.detect(content, context=f"群：{group_name}")
        action = detector.decide_action(ex)
        if action == ACTION_AUTO:
            logger.info("高置信度作业，自动加入 #%s：%s | %s", group_id, ex.subject, ex.deadline)
            await notifier.auto_add(msg, ex, group_name)
        elif action == ACTION_ASK:
            logger.info("识别为作业，待确认 #%s：%s | %s", group_id, ex.subject, ex.deadline)
            await notifier.ask(msg, ex, group_name)
        else:  # ACTION_DROP 静默丢弃
            logger.debug("低于置信度阈值，静默丢弃：%s | %s", ex.reason, content[:40])

    async def on_event(event: dict) -> None:
            if event.get("post_type") != "message":
                return
            mtype = event.get("message_type")
    
            if mtype == "group":
                await handle_group(event)
            elif mtype == "private":
                await notifier.handle_reply(event.get("user_id"), strip_cq(event.get("message", "")))

    client = OneBotClient(
        ws_url=qq_cfg["onebot_ws_url"],
        access_token=qq_cfg["access_token"],
        on_event=on_event,
    )

    bridge = SchedulerBridge(cfg["scheduler"]["endpoint"], cfg["scheduler"]["timeout"])

    notifier = Notifier(
        onebot=client,
        owner_id=owner_id,
        bridge=bridge,
        confirm_timeout=cfg["notifier"]["confirm_timeout_seconds"],
    )

    logger.info("服务启动，等待 QQ 群消息…")
    await client.run_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("已停止")
