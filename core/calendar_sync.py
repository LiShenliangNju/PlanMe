import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR / ".config"))

from settings import settings

import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import caldav
from caldav import Event, Todo

from schemas.schedule_schema import CalendarItemSchema


class iCloudCalendarManager:

    def __init__(self):
        self.tz = ZoneInfo(settings.TIMEZONE)
        # 从 os.environ["CALDAV_CONFIG_FILE"] 载入配置
        self.calendars = caldav.get_calendars()

    def _get_calendar_by_type(self, item_type: str):
        """
        日历自动路由规则：
        1. 有匹配时：
           - item_type == 'Event' -> 默认匹配 'planme' 日历
           - item_type == 'Todo'  -> 默认匹配 '提醒' 日历
        2. 未匹配到时降级为第一个可用的日历。
        """
        calendars = self.calendars
        if not calendars:
            raise RuntimeError("❌ 未能获取到任何 iCloud 日历")

        target_keyword = "planme" if item_type == "Event" else "提醒"

        for cal in calendars:
            if cal.name == target_keyword:
                return cal

        print(f"⚠️ 未找到匹配 '{target_keyword}' 的日历，降级使用 [{calendars[0].name}]")
        return calendars[0]

    def _generate_uid(self) -> str:
        """生成格式如 1785565120-f29d41 的 UID"""
        timestamp = int(time.time())
        short_uuid = uuid.uuid4().hex[:6]
        return f"{timestamp}-{short_uuid}"

    def create_item(self, item: CalendarItemSchema) -> str:
        """根据结构化数据创建日历日程Event或待办Todo

        Args:
            item: 遵循CalendarItemSchema规范的日历结构化对象
                - item_type: Event 或 Todo ； Event 为常规日程/会议（有持续时长），Todo 为待办/任务提醒（有截止时间）
                - summary: 事件/待办标题，从通知消息提取核心主题
                - start_time:
                    1. 格式强制为 YYYY-MM-DDTHH:MM:SS，示例：2026-08-02T06:00:00
                    2. 禁止携带时区标识（不要加+08:00、Z等时区后缀，时区由后端统一补充）
                    3. Event的日程开始时间；Todo的任务截止时间
                    4. 仅提供日期无具体时刻时：Event默认10:00:00，Todo默认20:00:00
                    5. 使用用户本地时间，不要输出UTC时间
                - duration_minutes: 持续时长（分钟）；仅Event生效；Todo类型请忽略此字段
                - location: 地点或会议链接，选填
                - url: 关联外部链接，选填
                - description: 详细备注信息，选填

        Returns:
            创建成功提示文本，包含目标日历名称与事项标题

        
        """
        calendar = self._get_calendar_by_type(item.item_type)

        dt_start = datetime.fromisoformat(item.start_time).replace(tzinfo=self.tz)
        dt_stamp = datetime.now(self.tz)
        uid = self._generate_uid()

        if item.item_type == "Event":
            duration = timedelta(minutes=item.duration_minutes or 60)
            calendar.add_object(
                Event,
                uid=uid,
                summary=item.summary,
                dtstamp=dt_stamp,
                dtstart=dt_start,
                duration=duration,
                location=item.location or "",
                url=item.url or "",
                description=item.description or "",
            )
            return f"已成功在【{calendar.name}】日历中添加日程: {item.summary}"

        elif item.item_type == "Todo":
            calendar.add_object(
                Todo,
                uid=uid,
                summary=item.summary,
                dtstamp=dt_stamp,
                due=dt_start,
                url=item.url or "",
                description=item.description or "",
            )
            return f"已成功在【{calendar.name}】提醒事项中添加待办: {item.summary}"

        else:
            raise ValueError(f"未知的类型: {item.item_type}")
