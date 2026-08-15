from typing import Optional
from pydantic import BaseModel, Field


class Sender(BaseModel):
    user_id: int
    nickname: str = ""
    card: str = ""  # 群名片
    role: str = "member"  # owner / admin / member


class GroupMessage(BaseModel):
    message_id: int
    group_id: int
    sender: Sender
    content: str
    time: int  # unix 时间戳


class HomeworkExtraction(BaseModel):
    """模型从消息中抽取出的结构化作业信息。"""

    is_homework: bool = Field(description="这条消息是否老师在布置作业/任务")
    subject: Optional[str] = Field(None, description="科目或课程名，如 高数/数据结构")
    deadline: Optional[str] = Field(
        None, description="截止时间，尽量还原为可解析的时间表达，如 2026-08-20 23:59 / 下周一"
    )
    description: Optional[str] = Field(None, description="作业内容摘要")
    confidence: float = Field(0.0, ge=0.0, le=1.0, description="判断置信度 0~1")
    reason: Optional[str] = Field(None, description="判断依据（便于排查误报）")


class ReminderPayload(BaseModel):
    """发给现有日程系统的结构化提醒请求。"""

    title: str
    deadline: Optional[str] = None
    description: Optional[str] = None
    source: str = "QQ群"
    raw: Optional[str] = None
