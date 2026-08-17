"""独立启动入口：python -m core.homework。

与 main.py 的 lifespan 启动等效，但作为独立进程运行（便于单独调试扫描器）。
路径注入已由 scanner.py 顶部兜底处理，这里只需保证能从项目根导入包。
"""
import asyncio
import logging
import signal
import sys
from pathlib import Path

# 确保项目根可被导入（无论从哪个 cwd 启动）
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.homework.scanner import HomeworkScanner

logger = logging.getLogger("planme.homework")


async def _main() -> None:
    scanner = HomeworkScanner(feed=None)
    await scanner.run()


def _handle_signal(signum, frame):
    logger.info("收到信号 %s，准备退出…", signum)
    raise KeyboardInterrupt()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # 支持 Ctrl+C / 终端关闭时优雅退出
    try:
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
    except (ValueError, OSError):
        pass  # 非主线程/Windows 限制时忽略

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("扫描器已停止。")
