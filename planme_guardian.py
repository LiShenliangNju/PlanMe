# -*- coding: utf-8 -*-
r"""
planme_guardian.py - Planme 单进程常驻调度器
====================================================================
用「一个」常驻进程替代原本散落在根目录的启动/关闭 bat：

  - 每天 12:40 / 22:00（本地时间）自动拉起 Ollama + NapCat
  - NapCat 通过 D:\Tools\NapCatQQ\NapCat.Shell\launcher.bat 启动，
    会弹出独立窗口提示用户扫码登录
  - Ollama 启动后 `ollama` 终端命令即可用
  - 服务启动后持续抓取 1 小时消息（运行 homework 扫描器监听 QQ 群）
  - 1 小时后自动关闭 Ollama + NapCat（+扫描器），避免多进程残留
  - 单例锁（.guardian.lock）保证全天只有一个常驻进程
  - 每个时段启动前清理占用 3001 的残留 NapCat，避免重复拉起

子命令（由 guardian.bat 分发）：
  run        常驻主循环（默认，通常由 guardian.bat start / 计划任务拉起）
  stop       写入停止标志，守护进程数秒内优雅退出并清理
  status     查看是否在运行、下一次触发时间
  test       自检：校验路径/端口/计划时间，不启动任何服务
  install    注册「登录后自动启动」的 Windows 计划任务（真正常驻）
  uninstall  删除上述计划任务
====================================================================
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# ====================== 可配置项（按需修改） ======================
ROOT = Path(__file__).resolve().parent
PYTHON_EXE = r"D:\Tools\Miniforge3\envs\planme\python.exe"        # 运行扫描器
PYTHONW_EXE = r"D:\Tools\Miniforge3\envs\planme\pythonw.exe"       # 常驻自身（无窗口）
NAPCAT_LAUNCHER = r"D:\Tools\NapCatQQ\NapCat.Shell\launcher.bat"

SCHEDULE = ["12:40", "22:00"]            # 本地时间，每天两个时段
CAPTURE_MIN = 60                         # 每个时段抓取时长（分钟）
RUN_SCANNER = True                       # 抓取窗口内是否运行 homework 扫描器
RUN_MAIN_SYSTEM = True                  # 如需把作业真正写入日历，设为 True 并确认下面端口
CLEAN_STALE_NAPCAT = True                # 时段启动前清理占用 3001 的残留 NapCat

OLLAMA_CMD = "ollama"                    # 拉起 ollama serve 的命令
OLLAMA_PORT = 11434
NAPCAT_WS_PORT = 3001
MAIN_PORT = 8000

LOCK_FILE = ROOT / ".guardian.lock"
STOP_FLAG = ROOT / ".guardian_stop"
LOG_FILE = ROOT / "guardian.log"
SCANNER_LOG = ROOT / "scanner.log"
MAIN_LOG = ROOT / "main.log"
TASK_NAME = "PlanmeGuardian"

# Windows 进程创建标志（兼容不同 Python 版本）
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)

# ====================== 日志 ======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("guardian")

# ====================== 全局状态 ======================
ACTIVE: dict[str, int | None] = {}   # 当前时段拉起的孩子进程 pid
STOP_EVENT = False                   # 通过信号或标志置位


def stop_requested() -> bool:
    if STOP_EVENT:
        return True
    return STOP_FLAG.exists()


def set_stop(*_args) -> None:
    global STOP_EVENT
    STOP_EVENT = True


# ====================== 进程工具 ======================
def is_pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但无权限发送信号 —— 仍视为存活
        return True
    except Exception:
        return False


def kill_tree(pid: int, label: str = "") -> None:
    if not pid:
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, timeout=15,
        )
        log.info("已终止进程树 pid=%s %s", pid, label)
    except Exception as e:  # noqa: BLE001
        log.warning("终止 pid=%s 失败: %s", pid, e)


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    import socket
    s = socket.socket()
    s.settimeout(1.0)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def owners_of_port(port: int):
    """返回占用该本地端口的进程 PID 列表（Windows）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue).OwningProcess"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        pids: set[int] = set()
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))
        return [p for p in pids if p]
    except Exception as e:  # noqa: BLE001
        log.warning("查询端口 %s 占用失败: %s", port, e)
        return []


def free_port(port: int, label: str = "") -> None:
    for pid in owners_of_port(port):
        kill_tree(pid, label)


def wait_port(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port):
            return True
        if stop_requested():
            return port_open(port)
        time.sleep(2)
    return port_open(port)


# ====================== 单例锁 ======================
def acquire_lock() -> bool:
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
        except Exception:
            pid = None
        if pid and is_pid_alive(pid):
            log.warning("守护进程已在运行 pid=%s，本实例退出", pid)
            return False
        log.info("发现过期锁 pid=%s，覆盖", pid)
    try:
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("写锁文件失败: %s", e)
    return True


def release_lock() -> None:
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except Exception:
        pass


# ====================== 服务启停 ======================
def start_ollama():
    if port_open(OLLAMA_PORT):
        log.info("Ollama 已在运行（端口 %s），复用，不接管其生命周期", OLLAMA_PORT)
        return None
    log.info("启动 Ollama 服务 …")
    try:
        proc = subprocess.Popen(
            [OLLAMA_CMD, "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("启动 Ollama 失败: %s", e)
        return None
    if wait_port(OLLAMA_PORT, timeout=60):
        log.info("Ollama 就绪（pid=%s），终端命令可用", proc.pid)
        return proc.pid
    log.warning("Ollama 启动后端口 %s 未就绪", OLLAMA_PORT)
    return None


def start_napcat():
    napcat_dir = str(Path(NAPCAT_LAUNCHER).parent)
    log.info("启动 NapCat（%s），将弹出扫码窗口", NAPCAT_LAUNCHER)
    try:
        proc = subprocess.Popen(
            ["cmd.exe", "/c", NAPCAT_LAUNCHER],
            cwd=napcat_dir,
            creationflags=CREATE_NEW_CONSOLE,
        )
        return proc.pid
    except Exception as e:  # noqa: BLE001
        log.warning("启动 NapCat 失败: %s", e)
        return None


def start_scanner():
    # homework 扫描器已并入主程序单一入口（main.py）：由 app 的 lifespan
    # 按 ENABLE_HOMEWORK 自动作为后台任务启动，不再单独拉起子进程。
    return None


def start_main():
    if not RUN_MAIN_SYSTEM:
        return None
    if port_open(MAIN_PORT):
        log.info("主系统已在运行（端口 %s），复用", MAIN_PORT)
        return None
    log.info("启动主系统 main.py（单一入口，扫描器随其自动拉起）…")
    try:
        logf = open(MAIN_LOG, "a", encoding="utf-8")
        env = dict(os.environ)
        env["ENABLE_HOMEWORK"] = "true" if RUN_SCANNER else "false"
        proc = subprocess.Popen(
            [PYTHON_EXE, "main.py"],
            cwd=str(ROOT),
            stdout=logf, stderr=logf,
            creationflags=CREATE_NO_WINDOW,
            env=env,
        )
        return proc.pid
    except Exception as e:  # noqa: BLE001
        log.warning("启动主系统失败: %s", e)
        return None


def teardown() -> None:
    pids = dict(ACTIVE)
    ACTIVE.clear()
    log.info("===== 进入清理：关闭本时段拉起的服务 =====")
    for key in ("scanner", "napcat", "ollama", "main"):
        pid = pids.get(key)
        if pid:
            kill_tree(pid, key)
    time.sleep(2)
    # 残留兜底：我们只清理自己接管的服务，外部管理的服务不强行杀
    if port_open(NAPCAT_WS_PORT) and pids.get("napcat"):
        log.warning("NapCat WS %s 仍未关闭（可能有残留），按端口兜底清理", NAPCAT_WS_PORT)
        free_port(NAPCAT_WS_PORT, "napcat-residual")
    log.info("===== 清理完成 =====")


# ====================== 单个时段 ======================
def run_cycle() -> None:
    log.info("########## 时段开始 ##########")

    # 防多进程残留：启动前清理占用 3001 的旧 NapCat
    if CLEAN_STALE_NAPCAT and port_open(NAPCAT_WS_PORT):
        log.info("发现占用 %s 的残留 NapCat，先清理", NAPCAT_WS_PORT)
        free_port(NAPCAT_WS_PORT, "stale-napcat")
        time.sleep(1)

    ACTIVE["ollama"] = start_ollama()
    ACTIVE["napcat"] = start_napcat()
    ACTIVE["main"] = start_main()

    # 等待 NapCat 正向 WS 就绪（用户需在此期间扫码登录）
    log.info(">>> 请在弹出的 NapCat 窗口中扫码登录 <<<")
    ws_ok = wait_port(NAPCAT_WS_PORT, timeout=180)
    if not ws_ok:
        log.warning("NapCat WS %s 在超时内未就绪，仍继续（未扫码则本时段无消息）", NAPCAT_WS_PORT)

    log.info("开始抓取消息，持续 %d 分钟 …", CAPTURE_MIN)
    end = time.time() + CAPTURE_MIN * 60
    while time.time() < end and not stop_requested():
        time.sleep(5)
    if stop_requested():
        log.info("收到停止信号，提前结束抓取")

    teardown()
    log.info("########## 时段结束 ##########")


def next_trigger_time(now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    cands: list[datetime] = []
    for t in SCHEDULE:
        h, m = (int(x) for x in t.split(":"))
        c = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if c <= now:
            c += timedelta(days=1)
        cands.append(c)
    return min(cands)


# ====================== 子命令 ======================
def cmd_run() -> None:
    if STOP_FLAG.exists():
        try:
            STOP_FLAG.unlink()
        except Exception:
            pass
    if not acquire_lock():
        print("守护进程已在运行，退出。", file=sys.stderr)
        return
    try:
        signal.signal(signal.SIGINT, set_stop)
        signal.signal(signal.SIGTERM, set_stop)
    except Exception:
        pass
    log.info("守护进程启动 pid=%s，计划时段=%s，抓取时长=%d 分钟",
             os.getpid(), SCHEDULE, CAPTURE_MIN)
    try:
        while not stop_requested():
            trig = next_trigger_time()
            delay = (trig - datetime.now()).total_seconds()
            log.info("下一次触发：%s（约 %.0f 分钟后）", trig.strftime("%H:%M"), delay / 60)
            while time.time() < trig.timestamp() and not stop_requested():
                time.sleep(min(5, trig.timestamp() - time.time()))
            if stop_requested():
                break
            try:
                run_cycle()
            except Exception as e:  # noqa: BLE001
                log.exception("时段执行异常：%s", e)
            time.sleep(2)
    finally:
        teardown()
        release_lock()
        try:
            STOP_FLAG.unlink()
        except Exception:
            pass
        log.info("守护进程退出")


def cmd_stop() -> None:
    try:
        STOP_FLAG.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
        print("已发送停止请求，守护进程将在数秒内退出并清理子进程。")
    except Exception as e:  # noqa: BLE001
        print("写停止标志失败:", e)


def cmd_status() -> None:
    pid = None
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
        except Exception:
            pid = None
    if pid and is_pid_alive(pid):
        print(f"守护进程运行中 pid={pid}")
        print(f"  下一次触发：{next_trigger_time().strftime('%Y-%m-%d %H:%M')}")
    else:
        print("守护进程未运行")
    if STOP_FLAG.exists():
        print("（存在停止标志，下次启动会被清除）")


def cmd_test() -> None:
    print("==== Planme 守护进程自检 ====")
    print(f"ROOT             : {ROOT}")
    print(f"PYTHON_EXE       : {PYTHON_EXE}  exists={Path(PYTHON_EXE).exists()}")
    print(f"PYTHONW_EXE      : {PYTHONW_EXE}  exists={Path(PYTHONW_EXE).exists()}")
    print(f"NAPCAT_LAUNCHER  : {NAPCAT_LAUNCHER}  exists={Path(NAPCAT_LAUNCHER).exists()}")
    print(f"计划时段         : {SCHEDULE}")
    print(f"抓取时长(分)     : {CAPTURE_MIN}")
    print(f"运行扫描器       : {RUN_SCANNER}")
    print(f"运行主系统       : {RUN_MAIN_SYSTEM}")
    print(f"Ollama 端口{OLLAMA_PORT} : {'已监听' if port_open(OLLAMA_PORT) else '未监听'}")
    print(f"NapCat WS{NAPCAT_WS_PORT}: {'已监听' if port_open(NAPCAT_WS_PORT) else '未监听'}")
    print(f"下一次触发       : {next_trigger_time().strftime('%Y-%m-%d %H:%M')}")
    print("（自检不启动任何服务）")


def cmd_install() -> None:
    tr = f'"{PYTHONW_EXE}" "{Path(__file__).resolve()}" run'
    log.info("注册计划任务：%s", tr)
    r = subprocess.run(
        ["schtasks", "/create", "/tn", TASK_NAME, "/tr", tr,
         "/sc", "onlogon", "/rl", "limited", "/f"],
        capture_output=True, text=True,
    )
    if r.stdout:
        print(r.stdout)
    if r.stderr:
        print(r.stderr, file=sys.stderr)
    if r.returncode == 0:
        print("已注册：登录后自动启动守护进程（常驻）。可用 `guardian.bat uninstall` 移除。")


def cmd_uninstall() -> None:
    r = subprocess.run(
        ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
        capture_output=True, text=True,
    )
    if r.stdout:
        print(r.stdout)
    if r.stderr:
        print(r.stderr, file=sys.stderr)
    print("已尝试移除计划任务。")


def main() -> None:
    p = argparse.ArgumentParser(description="Planme 单进程常驻调度器")
    p.add_argument(
        "cmd", nargs="?", default="run",
        choices=["run", "stop", "status", "test", "install", "uninstall"],
    )
    args = p.parse_args()
    {
        "run": cmd_run,
        "stop": cmd_stop,
        "status": cmd_status,
        "test": cmd_test,
        "install": cmd_install,
        "uninstall": cmd_uninstall,
    }[args.cmd]()


if __name__ == "__main__":
    main()
