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
import sys
import threading
import time
import webbrowser

# 确保日志/print 在重定向到文件时也能实时落盘
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

import uvicorn

from app.factory import create_app

app = create_app()

# uvicorn 监听端口，同时用于拼接自动打开的前端地址
PORT = int(os.getenv("PORT", "8000"))
UI_URL = f"http://localhost:{PORT}/ui/"


def _is_planme_process(cmdline: list[str]) -> bool:
    if not cmdline:
        return False
    joined = " ".join(cmdline).lower()
    return any(k in joined for k in ("main.py", "planme", "uvicorn"))


def _free_port(port: int) -> None:
    """若端口被旧的 Planme/uvicorn 进程占用，则终止它；若是其他进程则退出并提示。

    uvicorn --reload 模式下会同时存在 reloader 进程和 server 子进程；仅杀掉监听端口的
    server 子进程会被 reloader 立即重启，因此还要顺带清理所有 Planme/uvicorn 相关进程。
    """
    try:
        import psutil
    except ImportError:  # 降级：仅检测，不自动杀
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", port))
            except OSError:
                return
        print(f"[Planme] 端口 {port} 已被占用，但当前环境未安装 psutil，无法自动判断/终止旧进程。")
        print(f"[Planme] 请先手动释放端口 {port}，或执行 `pip install psutil` 后重试。")
        sys.exit(1)

    killed = set()
    other = []

    # 1) 先处理监听目标端口的进程
    for conn in psutil.net_connections(kind="inet"):
        if conn.laddr.port != port:
            continue
        if not conn.pid:
            continue
        try:
            p = psutil.Process(conn.pid)
            cmdline = p.cmdline() or []
            if _is_planme_process(cmdline):
                print(f"[Planme] 发现旧进程 PID {conn.pid} 占用端口 {port}，正在终止...")
                p.terminate()
                try:
                    p.wait(timeout=5)
                except psutil.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=3)
                killed.add(conn.pid)
            else:
                other.append((conn.pid, " ".join(cmdline)[:80]))
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            print(f"[Planme] 无权限查询/终止 PID {conn.pid}，请手动释放端口 {port}")
            sys.exit(1)

    if other:
        print(f"[Planme] 端口 {port} 被非 Planme 进程占用：")
        for pid, cmd in other:
            print(f"  PID {pid}: {cmd}")
        print("[Planme] 请先手动释放端口后重试。")
        sys.exit(1)

    # 2) 清理所有 Planme/uvicorn 相关 python 进程（含 uvicorn reloader），避免端口被重启
    my_pid = os.getpid()
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        if not p.info["name"] or "python" not in p.info["name"].lower():
            continue
        pid = p.info["pid"]
        if pid == my_pid or pid in killed:
            continue
        cmdline = p.info["cmdline"] or []
        if not _is_planme_process(cmdline):
            continue
        try:
            print(f"[Planme] 发现相关旧进程 PID {pid}，正在终止...")
            p.terminate()
            try:
                p.wait(timeout=5)
            except psutil.TimeoutExpired:
                p.kill()
                p.wait(timeout=3)
            killed.add(pid)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            print(f"[Planme] 无权限终止 PID {pid}，请手动释放端口 {port}")
            sys.exit(1)

    if killed:
        # 给操作系统一点时间回收端口
        time.sleep(0.5)
        print(f"[Planme] 已清理旧进程，继续启动...")


def _setup_logging() -> None:
    """配置运行日志：所有输出同时写控制台与 planme.log（追加模式，续写不重建）。

    - 幂等：重复调用（如 uvicorn --reload 的 worker 子进程重新导入本模块）不会重复包装；
    - 文件以 'a' 模式打开，每次启动续写历史日志而非清空；
    - 通过 Tee 包装 sys.stdout/stderr，控制台与文件同步输出；
    - uvicorn 仍走默认控制台日志，由 Tee 一并落盘，避免重复写入。
    """
    if getattr(sys.stdout, "_planme_teed", False):
        return

    import io
    from datetime import datetime

    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "planme.log")
    try:
        log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return

    class _Tee(io.TextIOBase):
        _planme_teed = True

        def __init__(self, console, file):
            self._console = console
            self._file = file

        def write(self, s):
            self._console.write(s)
            self._file.write(s)
            return len(s)

        def flush(self):
            self._console.flush()
            self._file.flush()

        @property
        def encoding(self):
            return getattr(self._console, "encoding", None) or "utf-8"

        def isatty(self):
            return getattr(self._console, "isatty", lambda: False)()

    sys.stdout = _Tee(sys.stdout, log_file)
    sys.stderr = _Tee(sys.stderr, log_file)

    sep = "=" * 60
    banner = (
        f"\n{sep}\n"
        f"  Planme 启动  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"{sep}\n"
    )
    sys.stdout.write(banner)
    sys.stdout.flush()


# 模块级安装日志：无论是 `python main.py` 入口进程，还是 uvicorn --reload 拉起的
# worker 子进程（会重新导入本模块），都会各自把输出追加到 planme.log。
_setup_logging()


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
    _free_port(PORT)
    threading.Thread(target=_wait_and_open_ui, daemon=True).start()
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
