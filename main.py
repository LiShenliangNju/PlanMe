"""Planme 唯一启动入口。

通过 app.factory.create_app() 创建 FastAPI 应用，所有路由在 api/__init__.py 集中注册，
所有后台服务（homework 扫描器等）由 app 的 lifespan 统一编排启停。

运行本文件（python main.py）会：
  1. 自动拉起 Streamlit Web 界面（web/app.py）并在浏览器打开；
  2. 启动 FastAPI 主系统（uvicorn，端口 8000）。

Web 界面默认随主系统一同启动；可通过环境变量 ENABLE_WEB=false 关闭。
"""
import atexit
import os
import signal
import socket
import subprocess
import sys

import uvicorn

from app.factory import create_app

app = create_app()

# Streamlit 默认监听端口，仅用于「是否已启动」探测（实际端口由 streamlit 自动分配）。
WEB_DEFAULT_PORT = int(os.getenv("WEB_PORT", "8501"))


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def _launch_web() -> None:
    """以子进程方式拉起 Streamlit Web 界面（非阻塞）。

    - 环境变量 ENABLE_WEB=false 时跳过；
    - 若 Web 端口已被占用（例如上一次残留进程未退出），不重复拉起，避免端口竞争；
    - 进程退出时自动终止该子进程，避免孤立的 streamlit 实例。
    """
    if os.getenv("ENABLE_WEB", "true").lower() == "false":
        return
    if _port_open(WEB_DEFAULT_PORT):
        print(f"[Planme] Web 端口 {WEB_DEFAULT_PORT} 已被占用，跳过重复启动 Streamlit。")
        return

    root = os.path.dirname(os.path.abspath(__file__))
    print("[Planme] 正在启动 Streamlit Web 界面 …")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "streamlit", "run", "web/app.py",
             "--server.headless", "false",
             "--browser.gatherUsageStats", "false"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[Planme] 启动 Web 失败（主系统仍正常运行）：{exc}")
        return

    def _cleanup() -> None:
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except Exception:
                pass

    atexit.register(_cleanup)
    print(f"[Planme] Web 界面即将在浏览器打开：http://localhost:{WEB_DEFAULT_PORT}")


if __name__ == "__main__":
    _launch_web()
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
