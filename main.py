"""Planme 唯一启动入口。

通过 app.factory.create_app() 创建 FastAPI 应用，所有路由在 api/__init__.py 集中注册，
所有后台服务（homework 扫描器等）由 app 的 lifespan 统一编排启停。

运行本文件（python main.py）会：
  1. 启动 FastAPI 主系统（uvicorn，端口 8000，默认 --reload 热重载）；
  2. 前端 SPA 已由 app.factory.create_app() 以静态目录挂载在 /ui，
     服务就绪后 main.py 自动在默认浏览器打开 http://localhost:8000/ui/。

Streamlit 过渡界面已移除（web/app.py 已删除），不再以子进程方式拉起；
可用环境变量 ENABLE_WEB=false 关闭「自动打开浏览器」（界面仍可通过 /ui 访问）。
"""
import os
import socket
import threading
import time
import webbrowser

import uvicorn

from app.factory import create_app

app = create_app()

# uvicorn 监听端口，同时用于拼接自动打开的前端地址
PORT = int(os.getenv("PORT", "8000"))
UI_URL = f"http://localhost:{PORT}/ui/"


def _wait_and_open_ui() -> None:
    """后台线程：等服务端口就绪后，自动用默认浏览器打开前端 UI。

    - 环境变量 ENABLE_WEB=false 时跳过自动打开；
    - 轮询端口直到可连（uvicorn 启动需要一点时间），最多等待 30 秒；
    - 以 daemon 线程运行，随主进程退出，无需额外清理。
    """
    if os.getenv("ENABLE_WEB", "true").lower() == "false":
        return

    deadline = time.time() + 30
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", PORT))
                break
            except OSError:
                time.sleep(0.5)
    else:
        print(f"[Planme] 等待后端端口 {PORT} 超时，未自动打开浏览器；请手动访问 {UI_URL}")
        return

    time.sleep(1.0)  # 给 uvicorn 一点时间完成路由绑定
    print(f"[Planme] 正在浏览器打开前端 UI：{UI_URL}")
    webbrowser.open(UI_URL, new=2)


if __name__ == "__main__":
    threading.Thread(target=_wait_and_open_ui, daemon=True).start()
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
