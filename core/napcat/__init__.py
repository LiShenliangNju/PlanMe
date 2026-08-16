"""NapCat 集成层：维护 qqbot 推送与建议日程的事件总线，供 API / Web 消费。"""
from .feed import FeedBus, FeedKind

__all__ = ["FeedBus", "FeedKind"]
