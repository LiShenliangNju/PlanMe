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


class HomeworkItem(BaseModel):
    """一条已落库、带决策状态的作业记录（Web 展示 / 历史回看的权威来源）。

    与 messages 表（仅存原始消息）不同，本表保存「识别结果 + 决策状态」。
    status 取值：pending（待确认）/ confirmed（已确认加入）/ auto（高置信自动加入）
               / ignored（已忽略）/ drop（非作业，静默丢弃）。
    """

    message_id: int
    cid: str = ""                 # 稳定的确认编号（hw{message_id}），跨重启不变
    group_id: int = 0
    group_name: str = ""
    user_id: int = 0
    is_homework: bool = False
    subject: Optional[str] = None
    deadline: Optional[str] = None
    description: Optional[str] = None
    confidence: float = 0.0
    status: str = "pending"       # pending / confirmed / auto / ignored / drop
    raw_content: Optional[str] = None
    created_at: int = 0           # 抓取时间（unix）
    decided_at: int = 0           # 决策/状态变更时间（unix）


class LectureNote(BaseModel):
    """白名单群里的图片经 OCR 后得到的 Markdown 笔记（讲座/通知存档）。"""

    message_id: int
    image_seq: int = 0            # 同一条消息里第几张图（从 0 开始），与 message_id 组成唯一键
    group_id: int = 0
    group_name: str = ""
    user_id: int = 0
    image_url: str = ""           # 原图 url（CQ 码里的 url）
    ocr_md: str = ""              # OCR 得到的 Markdown 文本
    status: str = "active"        # active（已存档）/ archived（已归档）/ error（OCR 失败）
    created_at: int = 0           # 抓取时间（unix）
    ocr_at: int = 0               # OCR 完成时间（unix）
