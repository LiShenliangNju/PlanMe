import sys
from pathlib import Path

# 载入配置
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / ".config"))

from settings import settings
from schemas.schedule_schema import CalendarItemSchema
from core.calendar_sync import iCloudCalendarManager

def test_add():
    """测试Event与Todo的创建"""
    calendar_manager = iCloudCalendarManager()
    
    EventItem = CalendarItemSchema(
        item_type = "Event",
        summary = "测试Event",
        start_time = "2026-08-20T06:00:00",
        duration_minutes = 30
    )

    TodoItem = CalendarItemSchema(
        item_type = "Todo", 
        summary = "测试Todo",
        start_time = "2026-08-20T20:00:00"
    )

    try:
        print(calendar_manager.create_item(EventItem))
        print(calendar_manager.create_item(TodoItem))
    except Exception as e:
        print(f"❌ 创建日历项目时出错: {e}")

if __name__ == "__main__":
    test_add()
