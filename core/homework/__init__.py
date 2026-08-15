"""QQ 群作业扫描子模块。

作为独立进程运行：`python -m core.homework`（依赖 NapCat + Ollama）。
识别老师发布的作业 -> 私聊主号确认 -> 经 HTTP 调用主系统 /api/chat 写入日程。
"""
