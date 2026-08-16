"""集中注册所有 API 路由。

新增功能模块时，只需在此处 `include_router` 对应 router，无需改动 main.py。
所有路由统一以 /api 为前缀（由各 router 自身的 prefix 决定）。
"""
from fastapi import FastAPI

from .schedule import router as schedule_router
from .homework import router as homework_router
from .napcat import router as napcat_router


def register_routers(app: FastAPI) -> None:
    app.include_router(schedule_router)   # /api/chat /api/manual-item /api/health
    app.include_router(homework_router)   # /api/homework/*
    app.include_router(napcat_router)     # /api/napcat/*
