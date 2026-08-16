import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR / ".config"))

from settings import settings

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from pydantic import ValidationError
from ollama import Client

from schemas.schedule_schema import CalendarItemSchema
from core.calendar_sync import iCloudCalendarManager
from core.ollama_gpu import inference_lock


class PlanmeAgent:

    def __init__(self):
        self.client = Client(host=settings.OLLAMA_HOST)
        self.model = settings.OLLAMA_MODEL
        self.max_retries = 3
        self.tools = {
            "create_item": iCloudCalendarManager().create_item
        }

    def _parse_response(self, message: dict) -> dict:
        """解析 Ollama 响应，优先提取 Tool Call，退化则返回文本"""
        tool_calls = message.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                func_name = tc.get("function", {}).get("name")
                if func_name in self.tools:
                    args = tc["function"].get("arguments", {})

                    # 如果模型按函数签名把入参嵌套在了 item 下，提取出内部字典
                    if "item" in args and isinstance(args["item"], dict):
                        args = args["item"]

                    return {
                        "type": "tool_call",
                        "args": args,
                    }

        content = message.get("content", "")
        return {"type": "text", "reply": content}

    async def process_query(self, user_text: str) -> dict:
        tz = ZoneInfo(settings.TIMEZONE)
        now_str = datetime.now(tz).strftime("%Y-%m-%dT%H:%M:%S%z")

        system_prompt = (
            f"你是 Planme 智能日程管家。当前本地时间：{now_str}。\n"
            "一旦识别到用户创建/记录日程或待办的意图，优先触发 Tool calling 来响应；若非日程请求，用自然语言回复。\n"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]

        print(f"\n{'='*50}\n🚀 [新请求抵达] 用户输入: {user_text}")

        for attempt in range(self.max_retries):
            print(
                f"--- 🔄 [Agent 尝试 {attempt + 1}/{self.max_retries}] 开始 ---"
            )
            try:
                start_time = time.time()

                # 抢全局 GPU 锁，与文本检测 / 图片 OCR 互斥，避免单卡并发 swap
                async with inference_lock:
                    response = await asyncio.to_thread(
                        self.client.chat,
                        model=self.model,
                        messages=messages,
                        tools=list(self.tools.values()),
                        options={
                            "num_ctx": 4096,
                            "num_gpu": 99,
                            "temperature": 0.0,
                            "num_predict": 512,
                            "repeat_penalty": 1.05,
                            "top_p": 0.9,
                        },
                    )

                elapsed = time.time() - start_time
                message = response.message.model_dump()

                print(f"⏱️  [耗时统计] 单次推理耗时: {elapsed:.2f} 秒")
                print(
                    f"🤖  [模型原始输出]:\n{json.dumps(message, ensure_ascii=False, indent=2)}"
                )

                parsed_response = self._parse_response(message)
                print(f"🔍  [解析结果类型]: {parsed_response.get('type')}")

                if parsed_response.get("type") == "tool_call":
                    args = parsed_response.get("args", {})

                    item_schema = CalendarItemSchema(**args)

                    print("✅  [Schema 校验通过] 提取到合法参数。")
                    return {
                        "type": "tool_call",
                        "args": item_schema.model_dump(),
                    }

                elif parsed_response.get("type") == "text":
                    print("✅  [退化为纯文本]: 模型未触发工具调用")
                    return parsed_response

            except ValidationError as e:
                error_msg = f"JSON 参数校验失败，请纠正参数后重新输出工具调用:\n{e.json()}"
                messages.append(message)
                messages.append({"role": "user", "content": error_msg})
                print(f"❌  [Schema 校验失败]: 准备喂回模型重试...\n{e.errors()}")

            except Exception as e:
                messages.append(message)
                messages.append(
                    {
                        "role": "user",
                        "content": f"执行工具时发生错误：{str(e)}，请检查参数后重试。",
                    }
                )
                print(f"❌  [发生异常]: 准备喂回模型重试...\n{str(e)}")

        print("🚨 [Agent 彻底失败] 已达到最大重试次数")
        raise ValueError("模型连续多次未能输出合法的结构化数据，请尝试简化你的描述或稍后再试。")