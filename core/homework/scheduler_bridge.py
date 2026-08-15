"""对接现有日程系统：把识别出的作业组成自然语言，POST 给其 /chat 接口。

现有后端契约（FastAPI）：
    POST /chat
    Request : {"text": "..."}                      # ChatRequest
    Response: {"status":"success"|"error"|"chat",  # ChatResponse
               "message": "...",
               "data": {...} | null}
后端会用 Ollama 解析 text 并自动创建 iCloud 日程，无需本服务再做结构化解析。
"""

import json
import logging
import urllib.request
from typing import Tuple

from schemas.homework_schema import ReminderPayload

logger = logging.getLogger("scheduler")


class SchedulerBridge:
    def __init__(self, endpoint: str, timeout: int) -> None:
        # endpoint 指向现有系统的 /chat 完整地址，如 http://127.0.0.1:8000/chat
        self.endpoint = endpoint
        self.timeout = timeout

    @staticmethod
    def _to_text(payload: ReminderPayload) -> str:
        """把结构化作业拼成一句自然语言，交给后端 /chat 解析。"""
        parts = []
        if payload.deadline:
            parts.append(f"截止 {payload.deadline}")
        parts.append(payload.title)
        if payload.description:
            parts.append(f"内容：{payload.description}")
        if payload.source:
            parts.append(f"（来源：{payload.source}）")
        text = "提醒我：" + "，".join(parts)
        if payload.raw:
            # 附带原文，供后端更准地抽取时间/科目
            text += f"\n原始消息：{payload.raw}"
        return text

    async def add_reminder(self, payload: ReminderPayload) -> Tuple[bool, str]:
        """提交作业给现有系统，返回 (是否成功, 后端回执消息)。"""
        text = self._to_text(payload)
        data = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            status = body.get("status")
            message = body.get("message", "")
            ok = status in ("success", "chat")  # 视后端实际返回调整
            logger.info("已提交 /chat：%s | 回执=%s", text[:60], status)
            return ok, message or ("✅ 已创建" if ok else "⚠️ 失败")
        except Exception as exc:  # noqa: BLE001
            logger.error("提交 /chat 失败：%s | %s", text[:60], exc)
            return False, f"提交失败：{exc}"
