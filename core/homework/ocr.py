"""图片 OCR：本地 qwen2.5vl 视觉模型，把图片转成 Markdown 文本。

调用方式遵循 Ollama Python SDK：在 `chat` 方法的 messages 里传入模型名，
并在 user 消息中带 `images`（图片本地路径或 base64）即可做多模态推理。
示例：
    client.chat(
        model="qwen2.5vl:7b",
        messages=[{"role": "user", "content": PROMPT, "images": ["/path/to/img.png"]}],
    )
"""

import asyncio
import logging
from typing import Optional

from ollama import Client

logger = logging.getLogger("ocr")

# 要求模型只输出 Markdown，不做解释、不增删内容。
_OCR_SYSTEM_PROMPT = (
    "你是一个文档 OCR 与排版助手。请仔细阅读图片，将其中所有可见文字、标题、列表、"
    "表格、公式等完整、准确地提取出来，并转换为结构清晰的 Markdown 格式输出。\n"
    "要求：\n"
    "1. 保留原有层级（标题用 # / ##，列表用 - 或 1.）；\n"
    "2. 表格用标准 Markdown 表格语法；\n"
    "3. 不要添加图片中不存在的内容，也不要省略信息；\n"
    "4. 只输出 Markdown 正文，不要任何解释、前缀或后缀（如『以下是…』）。"
)


class ImageOCR:
    """基于本地 Ollama 视觉模型的图片 OCR 封装，带简单节流。"""

    def __init__(
        self,
        host: str,
        model: str,
        temperature: float = 0.0,
        throttle_seconds: float = 3.0,
    ) -> None:
        self._client = Client(host=host)
        self.model = model
        self.temperature = temperature
        self._throttle = throttle_seconds
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def ocr(self, image_path: str, extra_hint: Optional[str] = None) -> str:
        """对单张图片做 OCR，返回 Markdown 文本。image_path 为本地路径。"""
        # 节流：避免短时间内大量图片并发打爆本地 GPU
        async with self._lock:
            now = asyncio.get_running_loop().time()
            wait = self._throttle - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = asyncio.get_running_loop().time()

        user_content = _OCR_SYSTEM_PROMPT
        if extra_hint:
            user_content += f"\n\n补充提示（仅用于理解，不要写进结果）：{extra_hint}"

        try:
            resp = await asyncio.to_thread(
                self._client.chat,
                model=self.model,
                messages=[
                    {"role": "user", "content": user_content, "images": [image_path]}
                ],
                options={"temperature": self.temperature},
            )
            return (resp.get("message", {}).get("content") or "").strip()
        except Exception as exc:
            logger.exception("OCR 失败（%s）：%s", image_path, exc)
            return ""
