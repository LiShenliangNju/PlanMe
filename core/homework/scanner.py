"""HomeworkScanner：QQ 群作业扫描服务（合并原 core/homework/__main__.py 入口逻辑）。

作为主程序的同进程后台任务运行：由 app 的 lifespan 调用 run() 拉起，
stop() 在 lifespan 关闭时调用。扫描器仍只把作业转成自然语言 POST 给主系统
/api/chat，不自己写 iCloud；其 qqbot 推送 / 建议日程通过 core.napcat.feed 暴露。
"""
import asyncio
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
import yaml

# 防御性注入 .config 到 sys.path（支持 python -m core.homework 独立启动）
_BASE_DIR = Path(__file__).resolve().parent.parent.parent
_CFG_DIR = str(_BASE_DIR / ".config")
if _CFG_DIR not in sys.path:
    sys.path.insert(0, _CFG_DIR)

from settings import settings
from core.homework.detector import HomeworkDetector, ACTION_AUTO, ACTION_ASK, ACTION_DROP
from core.homework.message_store import MessageStore
from core.homework.notifier import Notifier, make_cid
from core.homework.onebot_client import OneBotClient
from core.homework.ocr import ImageOCR
from core.ollama_gpu import inference_lock
from core.homework.scheduler_bridge import SchedulerBridge
from schemas.homework_schema import GroupMessage, Sender, HomeworkItem, LectureNote

logger = logging.getLogger("homework.scanner")

_CQ_RE = re.compile(r"\[CQ:[^\]]*\]")
_CQ_IMG_RE = re.compile(r"\[CQ:image,([^\]]*)\]")


def cq_escape(text: str) -> str:
    """CQ 码参数转义（顺序固定：& 必须先转，否则会二次转义）。"""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace(",", "&#44;")
    )


def cq_unescape(text: str) -> str:
    """CQ 码参数反转义。图片 url 里的 & 会被上报成 &amp;，不还原会导致下载失败。"""
    return (
        str(text)
        .replace("&#44;", ",")
        .replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&amp;", "&")
    )


def normalize_message(message) -> str:
    """把 OneBot 消息统一归一化成 CQ 码字符串。

    为什么必须有这一步：
    - 实时事件在 NapCat 默认配置下是 CQ 字符串；
    - 但 `get_group_msg_history` 返回的 message 是 segment 数组；
    - 若 NapCat 上报格式配成 array，实时事件也会是数组。
    原先 `_handle_group_images` 遇到非字符串直接 return，等于整条图片路失效。
    """
    if isinstance(message, str):
        return message
    if not isinstance(message, list):
        return ""
    parts: list[str] = []
    for seg in message:
        if not isinstance(seg, dict):
            continue
        stype = seg.get("type")
        data = seg.get("data") or {}
        if stype == "text":
            parts.append(str(data.get("text") or ""))
        elif stype == "image":
            kvs = []
            for key, val in (
                ("file", data.get("file")),
                ("url", data.get("url")),
                ("subType", data.get("sub_type") or data.get("subType")),
            ):
                if val:
                    kvs.append(f"{key}={cq_escape(val)}")
            parts.append(f"[CQ:image,{','.join(kvs)}]")
        elif stype == "at":
            parts.append(f"[CQ:at,qq={data.get('qq')}]")
        elif stype == "face":
            parts.append(f"[CQ:face,id={data.get('id')}]")
        elif stype:
            parts.append(f"[CQ:{stype}]")
    return "".join(parts)


def parse_cq_images(text: str) -> list[dict]:
    """从 CQ 字符串里提取所有图片段，返回 [{'file','url','subType'}, ...]。"""
    out: list[dict] = []
    for m in _CQ_IMG_RE.finditer(text):
        params: dict = {}
        for kv in m.group(1).split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                # 反转义：url 常含 &amp;，不还原会 404
                params[k.strip()] = cq_unescape(v.strip())
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
        self._image_throttle = 0.0
        self._image_max_concurrency = 1
        self._ocr = None
        # OCR 任务队列：图片先落库(pending)再由 worker 慢慢消费，杀进程不丢
        self._image_dir: Path | None = None
        self._ocr_retry_max = 2
        self._ocr_queue: asyncio.Queue | None = None
        self._ocr_workers: list[asyncio.Task] = []
        self._ocr_inflight: set[tuple[int, int]] = set()
        # 历史补抓（治「窗口外消息永久丢失」）
        self._catchup_enabled = True
        self._catchup_page_size = 50
        self._catchup_max_pages = 5
        self._catchup_limit = 200
        self._catchup_max_age_hours = 72
        self._catchup_include_all_groups = False
        self._catchup_min_interval = 300.0
        self._last_catchup = 0.0
        self._resumed = False
        self._group_name_cache: dict[int, str] = {}

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
        # 注意：串行已由 worker 数量（max_concurrency）严格保证，
        # throttle 只是「同一 worker 两次调用的最小间隔」，默认 0 即不额外空等。
        self._image_throttle = float(img_cfg.get("throttle_seconds", 0))
        self._image_max_concurrency = max(1, int(img_cfg.get("max_concurrency", 1)))
        self._ocr_retry_max = max(1, int(img_cfg.get("retry_max", 2)))
        img_dir = img_cfg.get("image_dir") or "data/lecture_images"
        img_dir_p = Path(img_dir)
        if not img_dir_p.is_absolute():
            img_dir_p = Path(settings.BASE_DIR) / img_dir_p
        img_dir_p.mkdir(parents=True, exist_ok=True)
        self._image_dir = img_dir_p

        # 历史补抓配置
        cu_cfg = cfg.get("catchup") or {}
        self._catchup_enabled = bool(cu_cfg.get("enabled", True))
        self._catchup_page_size = int(cu_cfg.get("page_size", 50))
        self._catchup_max_pages = int(cu_cfg.get("max_pages", 5))
        self._catchup_limit = int(cu_cfg.get("max_messages_per_group", 200))
        self._catchup_max_age_hours = float(cu_cfg.get("max_age_hours", 72))
        self._catchup_include_all_groups = bool(cu_cfg.get("include_all_groups", False))
        self._catchup_min_interval = float(cu_cfg.get("min_interval_seconds", 300))

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
            gpu_lock=inference_lock,
        )
        self._detector = detector

        ocr = ImageOCR(
            host=settings.OLLAMA_HOST,
            model=self._image_model,
            throttle_seconds=self._image_throttle,
            keep_alive=img_cfg.get("keep_alive", "30m"),
            num_ctx=int(img_cfg.get("num_ctx", 8192)),
            num_predict=int(img_cfg.get("num_predict", 2048)),
            timeout=float(img_cfg.get("request_timeout", 300)),
            gpu_lock=inference_lock,
        )
        self._ocr = ocr
        # 队列 + worker：并发度就等于 worker 数，不再另设 semaphore（避免双重锁）
        self._ocr_queue = asyncio.Queue()
        self._ocr_workers = [
            asyncio.create_task(self._ocr_worker(i))
            for i in range(self._image_max_concurrency)
        ]
        # 预热：把视觉模型提前装进显存并常驻，第一张图不用等冷启动（不阻塞启动）
        if self._image_whitelist and bool(img_cfg.get("warmup", True)):
            asyncio.create_task(ocr.warmup())

        bridge = SchedulerBridge(cfg["scheduler"]["endpoint"], cfg["scheduler"]["timeout"])
        client = OneBotClient(
            ws_url=qq_cfg["onebot_ws_url"],
            access_token=qq_cfg["access_token"],
            on_event=self._on_event,
            on_ready=self._on_ws_ready,
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
            # 先归一化：实时事件可能是 CQ 串或 segment 数组，补抓来的一定是数组
            event["message"] = normalize_message(event.get("message"))
            await self._handle_group(event)            # 作业识别
            await self._handle_group_images(event)    # 白名单群图片 OCR（独立白名单）
            await self._mark_progress(event)          # 推进断点（补抓的唯一依据）
        elif mtype == "private":
            await self._notifier.handle_reply(
                event.get("user_id"),
                self.strip_cq(normalize_message(event.get("message", ""))),
            )

    async def _mark_progress(self, event: dict) -> None:
        """记录「这个群处理到哪了」。push 模式不重放历史，没有这个锚点就无法补抓。

        对所有群消息都记录（不只作业群），因为图片群、非老师消息同样构成时间线；
        写入用 MAX() 保证单调递增，补抓重放较老消息时不会把锚点拽回去。
        """
        if self._store is None:
            return
        group_id = int(event.get("group_id", 0) or 0)
        if not group_id:
            return
        try:
            await self._store.mark_progress(
                group_id,
                int(event.get("message_id", 0) or 0),
                int(event.get("time", 0) or 0) or int(time.time()),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("推进群 %s 断点失败：%s", group_id, exc)

    # ------------------------------------------------------------------
    # 历史补抓：NapCat 是 push 模式，进程没连上的时段消息不会重放。
    # 守护进程每天只开固定窗口，窗口外的作业/海报原本会永久丢失。
    # 这里在 WS 就绪后，按 group_progress 的断点把空窗期消息补回来。
    # ------------------------------------------------------------------
    async def _on_ws_ready(self) -> None:
        """WS 连上后：① 恢复未跑完的 OCR ② 补抓空窗期消息。"""
        if not self._resumed:
            self._resumed = True
            try:
                await self._resume_pending_ocr()
            except Exception as exc:  # noqa: BLE001
                logger.exception("恢复 OCR 队列失败：%s", exc)
        if not self._catchup_enabled:
            return
        now = time.time()
        if now - self._last_catchup < self._catchup_min_interval:
            logger.debug("距上次补抓不足 %.0fs，本次跳过（防重连风暴）", self._catchup_min_interval)
            return
        self._last_catchup = now
        try:
            await self._catchup_all()
        except Exception as exc:  # noqa: BLE001
            logger.exception("历史补抓失败：%s", exc)

    async def _catchup_targets(self) -> list[int]:
        """补抓目标群：两个白名单 + 有断点记录的群（可选再并上全部群）。

        注意 qq.group_whitelist 为空表示「监听所有群」，但补抓不能凭空知道群号，
        所以默认只补「明确白名单 + 已见过的群」；要全量补抓需显式开
        catchup.include_all_groups（会一次性拉很多历史，谨慎）。
        """
        groups: set[int] = set()
        groups |= {int(g) for g in self._group_whitelist}
        groups |= {int(g) for g in self._image_whitelist}
        for p in await self._store.all_progress():
            gid = int(p.get("group_id") or 0)
            if gid:
                groups.add(gid)
        if self._catchup_include_all_groups:
            for g in await self._client.get_group_list():
                gid = int((g or {}).get("group_id") or 0)
                if gid:
                    groups.add(gid)
        return sorted(groups)

    async def _catchup_all(self) -> None:
        targets = await self._catchup_targets()
        if not targets:
            logger.info("补抓：暂无目标群（白名单为空且无历史断点），跳过")
            return
        logger.info("开始历史补抓，目标群 %s 个：%s", len(targets), targets)
        total = 0
        for gid in targets:
            if not self.running:
                break
            try:
                total += await self._catchup_group(gid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("补抓群 %s 失败：%s", gid, exc)
        logger.info("历史补抓完成，共重放 %s 条消息", total)
        if self.feed and total:
            self.feed.publish("status", f"⏪ 已补抓空窗期消息 {total} 条")

    async def _catchup_group(self, group_id: int) -> int:
        """补抓单个群：从断点往后翻页，取回未处理的消息并按时间正序重放。

        重放走的是同一个 _on_event 链路，去重完全依赖
        messages.message_id 主键与 lecture_notes 的 UNIQUE 约束，不会重复处理。
        """
        prog = await self._store.get_progress(group_id)
        last_time = int((prog or {}).get("last_time") or 0)
        # 兜底下限：首次运行或断点太老时，最多只回溯 max_age_hours，
        # 否则第一次启动会把几个月的群历史一次性灌给本地模型。
        floor_ts = int(time.time() - self._catchup_max_age_hours * 3600)
        cutoff = max(last_time, floor_ts)

        collected: dict[int, dict] = {}
        anchor: int | None = None
        for _ in range(self._catchup_max_pages):
            msgs = await self._client.get_group_msg_history(
                group_id, message_seq=anchor, count=self._catchup_page_size
            )
            if not msgs:
                break
            for m in msgs:
                if int(m.get("time") or 0) > cutoff:
                    mid = int(m.get("message_id") or 0)
                    if mid:
                        collected[mid] = m
            oldest = msgs[0]  # get_group_msg_history 已按 time 正序
            if int(oldest.get("time") or 0) <= cutoff:
                break  # 已经翻过断点，没必要再往前
            if len(collected) >= self._catchup_limit:
                break
            nxt = int(oldest.get("message_id") or oldest.get("message_seq") or 0)
            if not nxt or nxt == anchor:
                break
            anchor = nxt

        if not collected:
            logger.info("补抓群 %s：无新消息（断点 %s）", group_id, last_time or "首次")
            return 0

        ordered = sorted(collected.values(), key=lambda m: int(m.get("time") or 0))
        ordered = ordered[: self._catchup_limit]
        gname = await self._group_name(group_id)
        logger.info(
            "补抓群 %s（%s）：重放 %s 条消息（断点 %s 之后）",
            group_id, gname, len(ordered), last_time or "首次",
        )
        for m in ordered:
            if not self.running:
                break
            await self._on_event(self._history_to_event(m, group_id, gname))
        return len(ordered)

    async def _group_name(self, group_id: int) -> str:
        """取群名并缓存（历史消息里通常不带 group_name）。"""
        if group_id in self._group_name_cache:
            return self._group_name_cache[group_id]
        info = await self._client.get_group_info(group_id)
        name = str((info or {}).get("group_name") or group_id)
        self._group_name_cache[group_id] = name
        return name

    @staticmethod
    def _history_to_event(msg: dict, group_id: int, group_name: str) -> dict:
        """把历史消息补成与实时事件同构的 dict，从而复用同一条处理链路。"""
        ev = dict(msg)
        ev["post_type"] = "message"
        ev["message_type"] = "group"
        ev["group_id"] = int(msg.get("group_id") or group_id)
        ev["message"] = normalize_message(msg.get("message"))
        if group_name and not ev.get("group_name"):
            ev["group_name"] = group_name
        return ev

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
        """白名单群里的图片 → 落盘 + 落库(pending) → 入队，OCR 交给后台 worker。

        与作业扫描相互独立：仅对 image.group_whitelist 中的群生效；群内所有图片
        都视为讲座/通知，不要求发送人身份；白名单为空则整体禁用。

        关键顺序：**先落盘落库、再 OCR**。这样即使守护进程 taskkill、Ollama 崩掉、
        或队列还堵着几十张图，图片本身和任务记录都已在 db 里，重启会继续跑。
        """
        if not self._image_whitelist or self._ocr is None:
            return
        group_id = int(event.get("group_id", 0))
        if group_id not in self._image_whitelist:
            return
        message = normalize_message(event.get("message", ""))
        imgs = parse_cq_images(message)
        if not imgs:
            return

        group_name = event.get("group_name") or self._group_name_cache.get(group_id) or str(group_id)
        sender = event.get("sender", {}) or {}
        user_id = int(sender.get("user_id", 0) or 0)
        created_at = int(event.get("time", 0)) or int(time.time())
        message_id = int(event.get("message_id", 0))
        for seq, img in enumerate(imgs):
            await self._enqueue_image(
                img, seq, message_id, group_id, group_name, user_id, created_at
            )

    async def _enqueue_image(
        self,
        img: dict,
        seq: int,
        message_id: int,
        group_id: int,
        group_name: str,
        user_id: int,
        created_at: int,
    ) -> None:
        """把一张图片持久化 + 落库为 pending 任务并入队（不做 OCR）。"""
        key = (message_id, seq)
        if key in self._ocr_inflight:
            return
        # 1) 图片落盘到项目目录：NapCat 缓存随时可能被清，temp 目录重启也会没，
        #    要「重启可续」就必须自己持久化一份。
        src = await self._client.fetch_image_path(img.get("file"), img.get("url"))
        local_path = await self._persist_image(src, group_id, message_id, seq) if src else ""
        if not local_path:
            # 拿不到图也要留痕（老逻辑是直接 return，图片连记录都没有）
            logger.warning("图片获取失败，仍落库待重试（群 %s，seq=%s）", group_name, seq)

        # 2) 落库 pending（UNIQUE(message_id,image_seq) 天然去重：重复推送/补抓重放都安全）
        note = LectureNote(
            message_id=message_id,
            image_seq=seq,
            group_id=group_id,
            group_name=group_name,
            user_id=user_id,
            image_url=img.get("url", "") or "",
            local_path=local_path,
            ocr_md="",
            status="pending",
            attempts=0,
            created_at=created_at,
            ocr_at=0,
        )
        try:
            inserted = await self._store.save_lecture_note(note)
        except Exception as exc:  # noqa: BLE001
            logger.exception("落库图片任务失败：%s", exc)
            return
        if not inserted:
            return  # 这张图早已入库（可能已 OCR 完成），不重复排队

        # 3) 入队，交给 worker 慢慢 OCR
        self._ocr_inflight.add(key)
        assert self._ocr_queue is not None
        await self._ocr_queue.put(
            {
                "message_id": message_id,
                "image_seq": seq,
                "group_id": group_id,
                "group_name": group_name,
                "image_url": img.get("url", "") or "",
                "file": img.get("file"),
                "local_path": local_path,
                "attempts": 0,
            }
        )
        qsize = self._ocr_queue.qsize()
        logger.info("图片已入队待 OCR（群 %s，seq=%s，队列积压 %s）", group_name, seq, qsize)
        if self.feed:
            self.feed.publish(
                "lecture",
                f"🖼️ 新图片已存档，排队 OCR 中（群：{group_name}，队列 {qsize}）",
                meta={"group": group_name, "seq": seq, "queue": qsize},
            )

    async def _persist_image(
        self, src: str, group_id: int, message_id: int, seq: int
    ) -> str:
        """把图片复制到项目内持久目录，返回新路径；失败则退回原路径。"""
        if not src or self._image_dir is None:
            return src or ""
        try:
            ext = os.path.splitext(src)[1] or ".png"
            dst = self._image_dir / f"{group_id}_{message_id}_{seq}{ext}"
            if not (dst.exists() and dst.stat().st_size > 0):
                await asyncio.to_thread(shutil.copy2, src, dst)
            # 源是我们下载的临时文件则清掉，避免 temp 越堆越多
            if os.path.basename(src).startswith("planme_img_"):
                try:
                    os.remove(src)
                except OSError:
                    pass
            return str(dst)
        except Exception as exc:  # noqa: BLE001
            logger.warning("图片持久化失败（%s），退回原路径：%s", src, exc)
            return src

    # ------------------------------------------------------------------
    # OCR worker：队列消费者。并发度 = worker 数 = image.max_concurrency
    # ------------------------------------------------------------------
    async def _ocr_worker(self, wid: int) -> None:
        assert self._ocr_queue is not None
        while True:
            task = await self._ocr_queue.get()
            try:
                await self._run_ocr_task(task)
            except asyncio.CancelledError:
                # 被 taskkill / 关服打断：db 里仍是 pending，下次启动自动重新捞出来
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("OCR worker#%s 处理异常：%s", wid, exc)
            finally:
                self._ocr_inflight.discard((task["message_id"], task["image_seq"]))
                try:
                    self._ocr_queue.task_done()
                except ValueError:
                    pass

    async def _run_ocr_task(self, task: dict) -> None:
        """真正跑一张图的 OCR，并把结果回填到 db。"""
        mid = int(task["message_id"])
        seq = int(task["image_seq"])
        group_name = task.get("group_name") or str(task.get("group_id"))
        attempts = int(task.get("attempts") or 0)
        path = task.get("local_path") or ""

        # 图片不在了（NapCat 清缓存 / 落盘失败）→ 用 url 兜底重下
        if not path or not os.path.isfile(path):
            src = await self._client.fetch_image_path(task.get("file"), task.get("image_url"))
            if src:
                path = await self._persist_image(src, int(task.get("group_id", 0)), mid, seq)
                await self._store.update_lecture_local_path(mid, seq, path)
        if not path or not os.path.isfile(path):
            give_up = attempts + 1 >= self._ocr_retry_max
            await self._store.mark_lecture_ocr_failed(
                mid, seq, "图片文件缺失且重新下载失败", give_up=give_up
            )
            logger.warning("图片缺失，%s（群 %s，seq=%s）", "放弃" if give_up else "留待重试", group_name, seq)
            return

        started = time.time()
        md = await self._ocr.ocr(
            path,
            extra_hint="这是一张讲座/通知/海报类图片，请提取其中全部文字并保留标题、列表、表格等排版结构。",
        )
        cost = time.time() - started

        if md:
            await self._store.mark_lecture_ocr_done(mid, seq, md)
            logger.info(
                "OCR 完成（群 %s，seq=%s，%s字，%.1fs，剩余队列 %s）",
                group_name, seq, len(md), cost,
                self._ocr_queue.qsize() if self._ocr_queue else 0,
            )
            if self.feed:
                self.feed.publish(
                    "lecture",
                    f"✅ 讲座/通知 OCR 完成（群：{group_name}，{len(md)}字，{cost:.0f}s）",
                    meta={"group": group_name, "seq": seq},
                )
            return

        # 失败：留在 pending 等重试，超过上限才置 error
        attempts += 1
        give_up = attempts >= self._ocr_retry_max
        await self._store.mark_lecture_ocr_failed(mid, seq, "OCR 返回空内容", give_up=give_up)
        if give_up:
            logger.warning("OCR 连续失败 %s 次，放弃（群 %s，seq=%s）", attempts, group_name, seq)
            return
        logger.info("OCR 失败第 %s 次，重新入队（群 %s，seq=%s）", attempts, group_name, seq)
        task["attempts"] = attempts
        task["local_path"] = path
        self._ocr_inflight.add((mid, seq))
        assert self._ocr_queue is not None
        await self._ocr_queue.put(task)

    async def _resume_pending_ocr(self) -> None:
        """把库里所有 pending 的图片重新入队 —— 这就是「重启可续」。"""
        if self._store is None or self._ocr_queue is None:
            return
        rows = await self._store.pending_lecture_notes()
        restored = 0
        for r in rows:
            key = (int(r["message_id"]), int(r["image_seq"]))
            if key in self._ocr_inflight:
                continue
            if int(r.get("attempts") or 0) >= self._ocr_retry_max:
                continue
            self._ocr_inflight.add(key)
            await self._ocr_queue.put(
                {
                    "message_id": int(r["message_id"]),
                    "image_seq": int(r["image_seq"]),
                    "group_id": int(r.get("group_id") or 0),
                    "group_name": r.get("group_name") or "",
                    "image_url": r.get("image_url") or "",
                    "file": None,
                    "local_path": r.get("local_path") or "",
                    "attempts": int(r.get("attempts") or 0),
                }
            )
            restored += 1
        if restored:
            logger.info("恢复上次未完成的 OCR 任务 %s 个，继续处理", restored)
            if self.feed:
                self.feed.publish("status", f"♻️ 恢复 {restored} 个未完成的图片 OCR 任务")

    async def stop(self) -> None:
        self.running = False
        if self._client is not None:
            self._client.stop()
        for t in self._ocr_workers:
            t.cancel()
        for t in self._ocr_workers:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._ocr_workers = []
        if self._store is not None:
            try:
                await self._store.close()
            except Exception:  # noqa: BLE001
                pass
