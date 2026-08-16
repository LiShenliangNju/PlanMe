"""后台服务注册表（单例）。

职责：
- 持有各后台服务实例（如 homework 扫描器），提供统一的 start/stop 扩展点；
- 持有跨进程共享的事件总线 feed（core.napcat.feed.FeedBus），供 API / Web 消费。

新增后台服务时：在 ServiceManager 上挂一个属性（或写个 start_x/stop_x），
并在 app/factory.py 的 lifespan 中调用即可，无需改动路由。
"""
from typing import Optional

from core.napcat.feed import FeedBus


class ServiceManager:
    def __init__(self) -> None:
        self.homework = None            # HomeworkScanner 实例（按需由 lifespan 注入）
        self.feed: FeedBus = FeedBus()  # qqbot 推送 / 建议日程事件总线
        self._homework_task = None       # 扫描器后台任务句柄

    def register_homework(self, scanner) -> None:
        self.homework = scanner

    def set_homework_task(self, task) -> None:
        self._homework_task = task

    @property
    def homework_task(self):
        return self._homework_task


# 全局单例：路由与 lifespan 共享同一份状态
services = ServiceManager()
