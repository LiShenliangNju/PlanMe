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

from core.ollama_gpu import inference_lock

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
    """基于本地 Ollama 视觉模型的图片 OCR 封装。

    调用侧的两个关键优化（都直接影响单张耗时）：

    1. `keep_alive`：不传时 Ollama 默认 5 分钟后卸载模型，下一张图又要重新
       把 ~7GB 权重装回显存 —— 这正是「有时 37s、有时 22s」的主因。设成 30m
       让模型在抓取窗口内常驻，只有第一张付冷启动代价。
    2. `num_ctx`：VL 模型默认上下文（如 16384）会占掉大量 KV cache 显存，
       8GB 卡上直接把权重挤到 CPU（ollama ps 显示 22%/78% CPU/GPU），推理速度
       断崖下降。OCR 单图单轮对话用不到那么长，压到 8192 能把更多层留在 GPU。

    并发控制不在这里做：串行由 scanner 的 worker 数量（image.max_concurrency）
    严格保证，因此 throttle 默认 0，不再额外空等。
    """

    def __init__(
        self,
        host: str,
        model: str,
        temperature: float = 0.0,
        throttle_seconds: float = 0.0,
        keep_alive: str = "30m",
        num_ctx: int = 8192,
        num_predict: int = 2048,
        timeout: float = 300.0,
        gpu_lock: "asyncio.Lock | None" = None,
    ) -> None:
        self._client = Client(host=host, timeout=timeout)
        self.model = model
        self.temperature = temperature
        self.keep_alive = keep_alive
        self.num_ctx = num_ctx
        self.num_predict = num_predict
        self._throttle = throttle_seconds
        self._last_call = 0.0
        self._lock = asyncio.Lock()
        # 全局 GPU 推理锁：与文本检测 / 主系统对话互斥，避免单卡并发 swap
        self._gpu_lock = gpu_lock or inference_lock

    def _options(self) -> dict:
        return {
            "temperature": self.temperature,
            "num_ctx": self.num_ctx,
            "num_predict": self.num_predict,
        }

    async def warmup(self) -> bool:
        """预热：提前把模型装进显存并按 keep_alive 常驻，避免第一张图等冷启动。"""
        try:
            async with self._gpu_lock:
                await asyncio.to_thread(
                    self._client.chat,
                    model=self.model,
                    messages=[{"role": "user", "content": "ok"}],
                    options={"temperature": 0.0, "num_predict": 1},
                    keep_alive=self.keep_alive,
                )
            logger.info("OCR 模型已预热并常驻（%s，keep_alive=%s）", self.model, self.keep_alive)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("OCR 模型预热失败（%s）：%s", self.model, exc)
            return False

    async def ocr(self, image_path: str, extra_hint: Optional[str] = None) -> str:
        """对单张图片做 OCR，返回 Markdown 文本。image_path 为本地路径。"""
        # 节流：仅作为可选的「两次调用最小间隔」，默认 0。
        # 真正防并发打爆 GPU 靠 scanner 的 worker 数量，别在这里重复上锁空等。
        if self._throttle > 0:
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
            # 真正调模型前抢全局 GPU 锁，与文本检测 / 主系统对话互斥
            async with self._gpu_lock:
                resp = await asyncio.to_thread(
                    self._client.chat,
                    model=self.model,
                    messages=[
                        {"role": "user", "content": user_content, "images": [image_path]}
                    ],
                    options=self._options(),
                    keep_alive=self.keep_alive,
                )
            return (resp.get("message", {}).get("content") or "").strip()
        except Exception as exc:
            logger.exception("OCR 失败（%s）：%s", image_path, exc)
            return ""
