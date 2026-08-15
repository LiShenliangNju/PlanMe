"""私聊确认状态机：识别到作业后向主号发私聊，解析主号回复的确认/取消/改时间指令。"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from schemas.homework_schema import GroupMessage, HomeworkExtraction, ReminderPayload

from .onebot_client import OneBotClient
from .scheduler_bridge import SchedulerBridge

logger = logging.getLogger("notifier")


@dataclass
class PendingItem:
    cid: str
    extraction: HomeworkExtraction
    group_id: int
    group_name: str
    raw: str
    created_at: float = field(default_factory=time.time)


_CID_RE = re.compile(r"#([A-Za-z0-9]{4,})")
_CONFIRM = ("确认", "是", "yes", "y", "ok", "加", "加入", "提交", "行", "可以", "好")
_CANCEL = ("取消", "否", "no", "n", "忽略", "不要", "不加","不加入", "不提交", "不行", "不可以", "不好")
_CHANGE = ("改", "改为", "时间", "延期", "推迟","延后", "改期", "换时间")


class Notifier:
    def __init__(
        self,
        onebot: OneBotClient,
        owner_id: int,
        bridge: SchedulerBridge,
        confirm_timeout: float,
    ) -> None:
        self.onebot = onebot
        self.owner_id = int(owner_id)
        self.bridge = bridge
        self.confirm_timeout = confirm_timeout
        self._seq = 0
        self.pending: dict[str, PendingItem] = {}

    async def ask(self, msg: GroupMessage, extraction: HomeworkExtraction, group_name: str) -> None:
        self._seq += 1
        cid = f"{self._seq:04d}"
        self.pending[cid] = PendingItem(
            cid=cid,
            extraction=extraction,
            group_id=msg.group_id,
            group_name=group_name,
            raw=msg.content,
        )
        text = self._build_prompt(extraction, cid, group_name)
        try:
            await self.onebot.send_private_msg(self.owner_id, text)
            logger.info("已向主号推送作业确认 #%s：%s", cid, extraction.subject)
        except Exception as exc:
            logger.error("推送确认失败 #%s：%s", cid, exc)
        # 清理超时项
        await self._sweep_expired()

    async def auto_add(self, msg: "GroupMessage", extraction: HomeworkExtraction, group_name: str) -> None:
        """高置信度（>阈值）作业：不经主人确认，直接写入日历并通知主人。"""
        ex = extraction
        title = f"{ex.subject or '作业'}{(' · ' + ex.description) if ex.description else ''}"
        payload = ReminderPayload(
            title=title,
            deadline=ex.deadline,
            description=ex.description,
            source=f"QQ群: {group_name}",
            raw=msg.content,
        )
        ok, detail = await self.bridge.add_reminder(payload)
        result = "✅ 已自动加入日程" if ok else "⚠️ 自动提交失败"
        text = (
            f"{result}（高置信度自动添加，置信度 {ex.confidence:.2f}）\n"
            f"科目：{ex.subject or '未识别'}\n"
            f"截止：{ex.deadline or '未识别'}\n"
            f"内容：{ex.description or '（无）'}\n"
            f"{detail}"
        ).strip()
        try:
            await self.onebot.send_private_msg(self.owner_id, text)
            logger.info("高置信度作业已自动加入：%s | %s", ex.subject, ex.deadline)
        except Exception as exc:
            logger.error("自动添加通知失败：%s", exc)

    def _build_prompt(self, ex: HomeworkExtraction, cid: str, group_name: str) -> str:
        lines = [
            f"【QQ群作业待确认 #{cid}】来源群：{group_name}",
            f"科目：{ex.subject or '未识别'}",
            f"截止：{ex.deadline or '未识别'}",
            f"内容：{ex.description or '（无）'}",
            f"置信度：{ex.confidence:.2f}",
            "是否加入日程？回复：",
            "  y / n",
            "  改 （如：改 下周日 22:00）",
        ]
        return "\n".join(lines)

    async def handle_reply(self, from_user_id: int, text: str) -> None:
        if int(from_user_id) != self.owner_id:
            return
        text = text.strip()
        cid = self._match_cid(text)
        item = self.pending.get(cid) if cid else self._latest()
        if item is None:
            return

        low = text.lower()

        if any(k in low for k in _CANCEL) and not any(k in low for k in _CONFIRM):
            await self._cancel(item)
            return
        if any(k in low for k in _CHANGE):
            new_time = self._extract_new_time(text)
            if new_time:
                item.extraction.deadline = new_time
            await self._confirm(item)
            return
        if any(k in low for k in _CONFIRM):
            await self._confirm(item)
            return

        # 无法识别：提示
        await self.onebot.send_private_msg(
            self.owner_id,
            f"没看懂（#{item.cid}）。\n 可回复:\n {_CONFIRM}\n {_CANCEL}\n {_CHANGE}"
        )

    def _match_cid(self, text: str) -> Optional[str]:
        m = _CID_RE.search(text)
        return m.group(1) if m else None

    def _latest(self) -> Optional[PendingItem]:
        if not self.pending:
            return None
        return max(self.pending.values(), key=lambda i: i.created_at)

    @staticmethod
    def _extract_new_time(text: str) -> Optional[str]:
        for kw in _CHANGE:
            if kw in text:
                return text.split(kw, 1)[1].strip() or None
        return None

    async def _confirm(self, item: PendingItem) -> None:
        ex = item.extraction
        title = f"{ex.subject or '作业'}{(' · ' + ex.description) if ex.description else ''}"
        payload = ReminderPayload(
            title=title,
            deadline=ex.deadline,
            description=ex.description,
            source=f"QQ群: {item.group_name}",
            raw=item.raw,
        )
        ok, detail = await self.bridge.add_reminder(payload)
        result = "✅ 已加入日程" if ok else "⚠️ 提交失败"
        await self.onebot.send_private_msg(
            self.owner_id, f"{result}（#{item.cid}）\n{detail}".strip()
        )
        self.pending.pop(item.cid, None)

    async def _cancel(self, item: PendingItem) -> None:
        await self.onebot.send_private_msg(self.owner_id, f"已忽略（#{item.cid}）")
        self.pending.pop(item.cid, None)

    async def _sweep_expired(self) -> None:
        now = time.time()
        expired = [c for c, i in self.pending.items() if now - i.created_at > self.confirm_timeout]
        for cid in expired:
            item = self.pending.pop(cid)
            logger.info("确认超时，自动忽略 #%s", cid)
            try:
                await self.onebot.send_private_msg(self.owner_id, f"确认超时，已忽略（#{cid}）")
            except Exception:
                pass
