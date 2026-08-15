from pydantic import BaseModel, Field
from typing import Optional, Literal

class CalendarItemSchema(BaseModel):
    """日历结构化数据抽取规范，用于生成日历事件/待办
    Event：事件、日程、会议等，其start_time代表开始时间；Todo：有截止时间的待办任务，如作业、提醒等，其start_time代表截止时间
    """
    item_type: Literal["Event", "Todo"] = Field(
        ..., description="类型：Event 为常规日程/会议（有持续时长），Todo 为待办/任务提醒（有截止时间）；首字母大写"
    )
    summary: str = Field(..., description="事件/待办标题，从通知消息提取核心主题")
    start_time: str = Field(
        ...,
        description="""
        时间规则：
        1. 格式强制为 YYYY-MM-DDTHH:MM:SS，示例：2026-08-02T06:00:00
        2. 禁止携带时区标识（不要加+08:00、Z等时区后缀，时区由后端统一补充）
        3. Event的日程开始时间；Todo的任务截止时间
        4. 仅提供日期无具体时刻时：Event默认10:00:00，Todo默认20:00:00
        5. 使用用户本地时间，不要输出UTC时间
        """.strip().replace("\n", "")
    )
    duration_minutes: Optional[int] = Field(
        60, description="持续时长（分钟），**仅Event生效；Todo类型请忽略此字段**"
    )
    location: Optional[str] = Field(None, description="地点或会议链接")
    url: Optional[str] = Field(None, description="关联的 URL 链接")
    description: Optional[str] = Field(None, description="详细描述/备注信息")