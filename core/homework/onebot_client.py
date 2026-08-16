"""OneBot 11（NapCat）WebSocket 客户端。

OneBot 11 协议说明（与 OneBot 12 不同，无 opcode、无客户端心跳）：
- 连接：ws://host:port/?access_token=TOKEN
- 服务端→客户端 事件：纯 JSON，带 `post_type`
    - message 事件：post_type=="message"，按 message_type 区分 group / private
    - meta_event 事件：心跳/生命周期，可忽略
- 客户端→服务端 API 调用：{"action": "...", "params": {...}, "echo": N}
- 服务端→客户端 API 响应：{"status":"ok","retcode":0,"data":...,"echo":N}
"""

import asyncio
import json
import logging
import os
import tempfile
import urllib.request
from typing import Any, Awaitable, Callable, Optional

import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger("onebot")

EventHook = Callable[[dict], Awaitable[None]]


class OneBotClient:
    def __init__(
        self,
        ws_url: str,
        access_token: str,
        on_event: EventHook,
        reconnect_delay: float = 5.0,
    ) -> None:
        self.ws_url = ws_url
        self.access_token = access_token
        self.on_event = on_event
        self.reconnect_delay = reconnect_delay

        self._ws: Optional[ClientConnection] = None
        self._echo_counter = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._listen_task: Optional[asyncio.Task] = None
        self._stop = False

    async def run_forever(self) -> None:
        """持续连接并监听，断线自动重连。"""
        self._stop = False
        while not self._stop:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("OneBot 连接异常：%s，%ss 后重连", exc, self.reconnect_delay)
                await asyncio.sleep(self.reconnect_delay)

    async def _connect_once(self) -> None:
        url = self.ws_url
        if "?" in url:
            url += f"&access_token={self.access_token}"
        else:
            url += f"?access_token={self.access_token}"

        logger.info("连接 OneBot WS：%s", self.ws_url)
        async with websockets.connect(url) as ws:  # type: ignore[assignment]
            self._ws = ws
            logger.info("OneBot WS 已连接")
            await self._loop(ws)

    async def _loop(self, ws: ClientConnection) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("收到非 JSON 数据：%r", raw[:200])
                continue

            # API 响应（带 echo）
            if "echo" in msg and ("status" in msg or "retcode" in msg):
                echo = msg["echo"]
                fut = self._pending.pop(echo, None)
                if fut and not fut.done():
                    if msg.get("status") == "ok" and msg.get("retcode") == 0:
                        fut.set_result(msg.get("data"))
                    else:
                        fut.set_exception(
                            RuntimeError(f"API 失败：{msg.get('status')}/{msg.get('retcode')} {msg.get('msg')}")
                        )
                continue

            # 事件（带 post_type）
            if "post_type" in msg:
                asyncio.create_task(self._safe_dispatch(msg))

    async def _safe_dispatch(self, event: dict) -> None:
        try:
            await self.on_event(event)
        except Exception as exc:
            logger.exception("处理事件出错：%s", exc)

    async def send_action(self, action: str, params: dict) -> Any:
        """调用 OneBot API，返回 data 字段。"""
        if self._ws is None:
            raise RuntimeError("OneBot 未连接")
        self._echo_counter += 1
        echo = self._echo_counter
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        await self._ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
        return await asyncio.wait_for(fut, timeout=30)

    async def send_private_msg(self, user_id: int, text: str) -> Any:
        """向指定 QQ 号发送私聊消息（C2C）。"""
        return await self.send_action(
            "send_private_msg", {"user_id": int(user_id), "message": text}
        )

    # ------------------------------------------------------------------
    # 图片下载：NapCat 收到群图片后会在本地缓存。优先用 OneBot 标准
    # `get_image` 拿本地缓存路径；失败再用图片 url 直接 HTTP 下载到临时文件。
    # 返回本地图片路径（供 Ollama 视觉模型做 OCR）。拿不到返回 None。
    # ------------------------------------------------------------------
    async def get_image(self, file_id: str) -> Optional[str]:
        """通过 OneBot get_image 取 NapCat 本地缓存文件绝对路径。"""
        try:
            data = await self.send_action("get_image", {"file": file_id})
            path = (data or {}).get("file")
            if path and os.path.isfile(path):
                return path
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_image 失败（%s）：%s", file_id, exc)
        return None

    async def download_image_url(self, url: str) -> Optional[str]:
        """把图片 url 下载到临时文件，返回本地路径。"""
        if not url:
            return None
        try:
            suffix = os.path.splitext(url.split("?")[0])[1] or ".png"
            fd, path = tempfile.mkstemp(suffix=suffix, prefix="planme_img_")
            os.close(fd)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: urllib.request.urlretrieve(url, path),  # noqa: S310
            )
            if os.path.isfile(path):
                return path
        except Exception as exc:  # noqa: BLE001
            logger.warning("下载图片失败（%s）：%s", url, exc)
        return None

    async def fetch_image_path(self, file_id: str, url: str) -> Optional[str]:
        """综合获取图片本地路径：先 get_image 缓存，再 url 兜底。"""
        path = await self.get_image(file_id)
        if path:
            return path
        return await self.download_image_url(url)

    def stop(self) -> None:
        self._stop = True
