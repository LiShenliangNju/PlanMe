"""Planme 唯一启动入口。

通过 app.factory.create_app() 创建 FastAPI 应用，所有路由在 api/__init__.py 集中注册，
所有后台服务（homework 扫描器等）由 app 的 lifespan 统一编排启停。
"""
import uvicorn

from app.factory import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
