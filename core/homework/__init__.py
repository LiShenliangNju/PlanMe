"""QQ 群作业扫描子模块（作为主程序同进程后台服务运行）。

HomeworkScanner 由 app 的 lifespan 启动 / 停止；扫描器只把作业转自然语言 POST 给主系统，
不自己写 iCloud。私聊确认状态机与 qqbot 推送通过 core.napcat.feed 暴露给 API / Web。
"""
from .scanner import HomeworkScanner

__all__ = ["HomeworkScanner"]
