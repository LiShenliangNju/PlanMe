"""HomeworkScanner：QQ 群作业扫描服务（合并原 core/homework/__main__.py 入口逻辑）。

作为主程序的同进程后台任务运行：由 app 的 lifespan 调用 run() 拉起，
stop() 在 lifespan 关闭时调用。扫描器仍只把作业转成自然语言 POST 给主系统
/api/chat，不自己写 iCloud；其 qqbot 推送 / 建议日程通过 core.napcat.feed 暴露。
"""
import asyncio
import logging
import re
import time
from pathlib import Path
import yaml

from settings import settings
from core.homework.detector import HomeworkDetector, ACTION_AUTO, ACTION_ASK, ACTION_DROP
from core.homework.message_store import MessageStore
from core.homework.notifier import Notifier, make_cid
from core.homework.onebot_client import OneBotClient
from core.homework.ocr import ImageOCR
from core.homework.scheduler_bridge import SchedulerBridge
from schemas.homework_schema import GroupMessage, Sender, HomeworkItem, LectureNote

logger = logging.getLogger("homework.scanner")

_CQ_RE = re.compile(r"\[CQ:[^\]]*\]")
_CQ_IMG_RE = re.compile(r"\[CQ:image,([^\]]*)\]")


def parse_cq_images(text: str) -> list[dict]:
    """从 CQ 字符串里提取所有图片段，返回 [{'file','url','subType'}, ...]。"""
    out: list[dict] = []
    for m in _CQ_IMG_RE.finditer(text):
        params: dict = {}
        for kv in m.group(1).split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                params[k.strip()] = v.strip()
        out.append(
            {
                "file": params.get("file"),
                "url": params.get("url"),
                "subType": params.get("subType"),
            }
        )
    return out


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
        # 图片 OCR 相关（独立白名单）
        self._image_whitelist = set()
        self._image_model = "qwen2.5vl:7b"
        self._image_throttle = 3.0
        self._image_max_concurrency = 2
        self._ocr = None
        self._image_sem = None

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
        self._group_whitelist = set(q for q in (qq_cfg.get("group_whitelist") or []))
        self._teacher_ids = set(int(x) for x in (qq_cfg.get("teacher_user_ids") or []))
        self._teacher_roles = set(qq_cfg.get("teacher_roles") or [])

        # 图片 OCR 配置（独立白名单，与作业扫描互不干扰）
        img_cfg = cfg.get("image") or {}
        self._image_whitelist = set(int(x) for x in (img_cfg.get("group_whitelist") or []))
        self._image_model = img_cfg.get("model", "qwen2.5vl:7b")
        self._image_throttle = float(img_cfg.get("throttle_seconds", 3))
        self._image_max_concurrency = int(img_cfg.get("max_concurrency", 2))

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

        ocr = ImageOCR(
            host=settings.OLLAMA_HOST,
            model=self._image_model,
            throttle_seconds=self._image_throttle,
        )
        self._ocr = ocr
        self._image_sem = asyncio.Semaphore(self._image_max_concurrency)

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
            store=store,
        )
        self._notifier = notifier
        # 进程重启后，把仍为 pending 的作业恢复进内存，主人仍可确认
        await self._notifier.rehydrate_from_db(store)

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
            await self._handle_group(event)            # 作业识别
            await self._handle_group_images(event)    # 白名单群图片 OCR（独立白名单）
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
        # 落库：识别结果 + 决策状态（Web 展示 / 历史回看权威来源）
        status = {ACTION_AUTO: "auto", ACTION_ASK: "pending", ACTION_DROP: "drop"}[action]
        await self._store.save_homework_item(
            HomeworkItem(
                message_id=msg.message_id,
                cid=make_cid(msg.message_id),
                group_id=group_id,
                group_name=group_name,
                user_id=user_id,
                is_homework=ex.is_homework,
                subject=ex.subject,
                deadline=ex.deadline,
                description=ex.description,
                confidence=ex.confidence,
                status=status,
                raw_content=content,
                created_at=int(event.get("time", 0)) or int(time.time()),
                decided_at=int(time.time()),
            )
        )
        if action == ACTION_AUTO:
            logger.info("高置信度作业，自动加入 #%s：%s | %s", group_id, ex.subject, ex.deadline)
            await self._notifier.auto_add(msg, ex, group_name)
        elif action == ACTION_ASK:
            logger.info("识别为作业，待确认 #%s：%s | %s", group_id, ex.subject, ex.deadline)
            await self._notifier.ask(msg, ex, group_name)
        else:
            logger.debug("低于置信度阈值，静默丢弃：%s | %s", ex.reason, content[:40])

    async def _handle_group_images(self, event: dict) -> None:
        """白名单群里的图片 → 抓取 → OCR → 存 lecture_notes。与作业扫描相互独立。

        仅对 image.group_whitelist 中的群生效；群内所有图片都视为讲座/通知，
        不要求发送人身份。image.group_whitelist 为空则整体禁用。
        """
        if not self._image_whitelist or self._ocr is None:
            return
        group_id = int(event.get("group_id", 0))
        if group_id not in self._image_whitelist:
            return
        message = event.get("message", "")
        if not isinstance(message, str):
            return  # 仅支持 CQ 字符串格式
        imgs = parse_cq_images(message)
        if not imgs:
            return

        group_name = event.get("group_name") or str(group_id)
        sender = event.get("sender", {})
        user_id = int(sender.get("user_id", 0))
        created_at = int(event.get("time", 0)) or int(time.time())
        message_id = int(event.get("message_id", 0))
        for seq, img in enumerate(imgs):
            asyncio.create_task(
                self._process_image(
                    img, seq, message_id, group_id, group_name, user_id, created_at
                )
            )

    async def _process_image(
        self,
        img: dict,
        seq: int,
        message_id: int,
        group_id: int,
        group_name: str,
        user_id: int,
        created_at: int,
    ) -> None:
        """抓取单张图片 → OCR → 落库 lecture_notes。"""
        path = await self._client.fetch_image_path(img.get("file"), img.get("url"))
        if not path:
            logger.warning("图片获取失败，跳过（群 %s，seq=%s）", group_name, seq)
            return
        now = int(time.time())
        try:
            async with self._image_sem:
                md = await self._ocr.ocr(
                    path,
                    extra_hint="这是一张讲座/通知/海报类图片，请提取其中全部文字并保留标题、列表、表格等排版结构。",
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("OCR 异常（群 %s，seq=%s）：%s", group_name, seq, exc)
            md = ""

        note = LectureNote(
            message_id=message_id,
            image_seq=seq,
            group_id=group_id,
            group_name=group_name,
            user_id=user_id,
            image_url=img.get("url", "") or "",
            ocr_md=md,
            status="active" if md else "error",
            created_at=created_at,
            ocr_at=now,
        )
        try:
            inserted = await self._store.save_lecture_note(note)
        except Exception as exc:  # noqa: BLE001
            logger.exception("保存讲座笔记失败：%s", exc)
            return
        if inserted and md and self.feed:
            self.feed.publish(
                "lecture",
                f"🖼️ 新讲座/通知（群：{group_name}）已 OCR 存档",
                meta={"group": group_name, "seq": seq},
            )
        logger.info("图片 OCR 完成（群 %s，seq=%s，%s字）", group_name, seq, len(md))

    async def stop(self) -> None:
        self.running = False
        if self._client is not None:
            self._client.stop()
        if self._store is not None:
            try:
                await self._store.close()
            except Exception:  # noqa: BLE001
                pass
