"""NapCat 集成层：qqbot 推送与建议日程的内存事件总线。

扫描器的 notifier 把「qqbot 推送 / 待确认 / 已自动添加 / 已确认 / 已忽略 / 状态变更」
发布到这里；API（api/homework.py、api/napcat.py）与 Web（web/app.py）消费同一份数据，
从而在 Web 上呈现 napcat 窗口。

设计为进程内、线程/协程安全的简单环形缓冲（CPython GIL 下 list.append 原子）。
未来若需跨进程，可替换为 Redis 等外部总线的同一接口。
"""

import time
from enum import Enum
from typing import Optional


class FeedKind(str, Enum):
    PUSH = "push"            # qqbot 发出的私聊推送
    PENDING = "pending"      # 新待确认作业
    AUTO_ADD = "auto_add"    # 高置信度自动加入
    CONFIRMED = "confirmed"  # 主人确认加入
    CANCELLED = "cancelled"  # 主人忽略
    STATUS = "status"        # 连接 / 状态变更


class FeedBus:
    def __init__(self, maxlen: int = 200) -> None:
        self._events: list[dict] = []
        self._maxlen = maxlen

    def publish(
        self,
        kind: str,
        text: str,
        cid: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> None:
        self._events.append(
            {"kind": kind, "text": text, "cid": cid, "meta": meta or {}, "ts": time.time()}
        )
        if len(self._events) > self._maxlen:
            self._events = self._events[-self._maxlen:]

    def recent(self, limit: int = 50, kinds=None) -> list[dict]:
        evs = self._events
        if kinds:
            evs = [e for e in evs if e["kind"] in kinds]
        return evs[-limit:]
