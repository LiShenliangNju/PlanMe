"""作业识别：关键词预过滤 + Ollama 本地模型结构化抽取。"""

import asyncio
import datetime
import json
import logging
from typing import Optional
from ollama import Client

from schemas.homework_schema import HomeworkExtraction
from core.ollama_gpu import inference_lock

logger = logging.getLogger("detector")

_SYSTEM_PROMPT = (
    "你是助教助手。判断一条 QQ 群消息是否是老师/班委发布的「作业或任务」，"
    "如果是，抽取科目、截止时间、内容摘要。\n"
    "规则：\n"
    "1. 只有明确布置给学生的作业、实验、论文、预习/复习、考试/测验安排才算作业；闲聊、通知、接龙、投票、普通问答不算。\n"
    "2. deadline 尽量还原成可解析的时间表达（如 '2026-08-20 23:59'、'下周一'、'本周日 22:00'）。\n"
    "3. confidence 表示你判断「这是真实作业」的把握（0~1），请按如下标准自评：\n"
    "   - < 0.6：你并不确定，本条可能被直接丢弃，不要给这么高；\n"
    "   - 0.6 ~ 0.9：较有把握，会发给你主人确认是否加入日程；\n"
    "   - > 0.9：非常有把握（截止时间、内容都明确，且明显是老师/班委布置），会不经确认直接加入日历。\n"
    "   只有截止时间、内容都清晰、且明显是布置作业/任务时才给 > 0.9。\n"
    "只输出符合 schema 的 JSON。"
)

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

ACTION_DROP = "drop"
ACTION_ASK = "ask"
ACTION_AUTO = "auto"


def _build_system_prompt() -> str:
    """动态把当前真实日期注入 prompt，避免模型把相对日期归到错误年份/星期。"""
    now = datetime.datetime.now()
    weekday = _WEEKDAYS[now.weekday()]
    return (
        _SYSTEM_PROMPT
        + f"\n当前真实日期是 {now.year}年{now.month}月{now.day}日（{weekday}）。"
        + f"涉及『X月Y日』『本周X』『下周一』等相对日期时，必须以当前年份 "
        f"{now.year}年 为基准推算绝对日期；若抽出的 deadline 年份明显早于今年，"
        "请修正为本年度对应日期（例如『8月22日』应理解为 "
        f"{now.year}年8月22日，而非更早的年份）。"
    )


class HomeworkDetector:
    def __init__(
        self,
        host: str,
        model: str,
        temperature: float,
        keyword_prefilter: list[str],
        throttle_seconds: float,
        min_confidence: float,
        auto_confidence: float = 0.9,
        gpu_lock: "asyncio.Lock | None" = None,
    ) -> None:
        self._client = Client(host=host)
        self.model = model
        self.temperature = temperature
        self.keywords = keyword_prefilter
        self.min_confidence = min_confidence
        self.auto_confidence = auto_confidence
        self._throttle = throttle_seconds
        self._last_call = 0.0
        self._lock = asyncio.Lock()
        # 全局 GPU 推理锁：与图片 OCR / 主系统对话互斥，避免单卡并发 swap
        self._gpu_lock = gpu_lock or inference_lock

    def prefilter(self, text: str) -> bool:
        """命中任一关键词才值得调模型。"""
        return any(kw in text for kw in self.keywords)

    async def detect(self, text: str, context: Optional[str] = None) -> HomeworkExtraction:
        """调用 Ollama 做结构化抽取，带节流。"""
        async with self._lock:
            now = asyncio.get_running_loop().time()
            wait = self._throttle - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = asyncio.get_running_loop().time()

        user_content = text
        if context:
            user_content = f"【上下文/来源】{context}\n【消息内容】{text}"

        try:
            # 真正调模型前抢全局 GPU 锁，与 OCR / 主系统对话互斥（一个跑另一个 pending）
            async with self._gpu_lock:
                resp = await asyncio.to_thread(
                    self._client.chat,
                    model=self.model,
                    messages=[
                        {"role": "system", "content": _build_system_prompt()},
                        {"role": "user", "content": user_content},
                    ],
                    format=HomeworkExtraction.model_json_schema(),
                    options={"temperature": self.temperature},
                )
            content = resp["message"]["content"]
            data = json.loads(content)
            result = HomeworkExtraction(**data)
        except Exception as exc:
            logger.exception("Ollama 识别失败：%s", exc)
            return HomeworkExtraction(is_homework=False, confidence=0.0, reason=f"模型调用异常:{exc}")

        # 漏报兜底：模型偶尔会把明显是作业的消息标成非作业，但字段已抽全。
        # 若关键词预过滤命中（说明消息本身带作业特征词），且模型已抽出截止时间，
        # 则信任抽取结果，仍判为作业，避免真作业被静默漏掉。
        if not result.is_homework and self.prefilter(text) and result.deadline:
            result.is_homework = True
            result.confidence = max(result.confidence, self.min_confidence)
            result.reason = (result.reason or "") + " (关键词命中+已抽到期末时间，兜底判为作业)"

        return result

    def decide_action(self, ex: "HomeworkExtraction") -> str:
        """根据识别结果决定处理分支：静默丢弃 / 询问主人 / 自动加入日历。

        路由标准（与 system prompt 告知模型的标准一致）：
          - 非作业 或 confidence < min_confidence(0.6)        -> drop（静默丢弃）
          - min_confidence(0.6) <= confidence <= auto(0.9)    -> ask（询问主人）
          - confidence > auto_confidence(0.9)                 -> auto（直接写日历）
        """
        if not ex.is_homework:
            return ACTION_DROP
        if ex.confidence < self.min_confidence:
            return ACTION_DROP
        if ex.confidence > self.auto_confidence:
            return ACTION_AUTO
        return ACTION_ASK
