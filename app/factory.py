"""应用工厂：集中创建 FastAPI 应用并编排所有后台服务。

- CORS 配置
- register_routers(app)：集中挂载所有 API 路由（新增模块在此 include）
- lifespan 事件处理器（标准写法，替代已废弃的 @app.on_event）：单一入口启动 / 停止所有后台服务

注意：使用 lifespan 上下文管理器（而非已废弃的 @app.on_event）统一编排后台服务启停；
标准 @asynccontextmanager 写法在本机 FastAPI 0.140 / Starlette 1.3.1 下可正常绑定并触发。
"""
import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# 确保 .config 在 sys.path（app 包导入即注入；此处兜底）
BASE_DIR = Path(__file__).resolve().parent.parent
_CONFIG_DIR = str(BASE_DIR / ".config")
if _CONFIG_DIR not in sys.path:
    sys.path.insert(0, _CONFIG_DIR)

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from settings import settings
from app.services import services
from api import register_routers

logger = logging.getLogger("planme")


async def _startup_services() -> None:
    """启动后台服务（扫描器等）。在 lifespan 的启动阶段调用。"""
    if not getattr(settings, "ENABLE_HOMEWORK", True):
        return
    try:
        from core.homework import HomeworkScanner
        scanner = HomeworkScanner(feed=services.feed)
        services.register_homework(scanner)
        services.set_homework_task(asyncio.create_task(scanner.run()))
        logger.info("🚀 [启动] homework 扫描器已作为后台任务拉起")
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ [启动] homework 扫描器启动失败（主系统继续运行）：%s", exc)


async def _shutdown_services() -> None:
    """停止后台服务。在 lifespan 的关闭阶段调用。"""
    if services.homework is not None:
        try:
            await services.homework.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ [停止] homework 扫描器停止异常：%s", exc)
    task = services.homework_task
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    """单一入口生命周期：启动编排所有后台服务，关闭时优雅停止。"""
    await _startup_services()
    yield
    await _shutdown_services()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Planme AI Agent API",
        description="基于 Ollama + CalDAV 的智能日程管家（主系统 + 可插拔后台服务）",
        version="1.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_routers(app)

    # 挂载前端 Web App（严格还原设计稿的 SPA）：python main.py 后访问 http://localhost:8000/ui
    frontend_dir = BASE_DIR / "web" / "frontend"
    if frontend_dir.exists():
        from fastapi.staticfiles import StaticFiles
        app.mount("/ui", StaticFiles(directory=str(frontend_dir), html=True), name="ui")

    return app
