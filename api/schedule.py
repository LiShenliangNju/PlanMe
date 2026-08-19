from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import traceback

from core.llm_agent import PlanmeAgent
from core.calendar_sync import iCloudCalendarManager
from schemas.schedule_schema import CalendarItemSchema

router = APIRouter(prefix="/api", tags=["Schedule API"])

agent = PlanmeAgent()
calendar_manager = iCloudCalendarManager()

class ChatRequest(BaseModel):
    text: str
    history: Optional[list] = None   # 多轮对话历史（前端渲染过的对话，回灌给模型做上下文）

class ChatResponse(BaseModel):
    status: str                         # "chat" | "success" | "error"
    message: str
    data: Optional[CalendarItemSchema] = None

@router.post("/chat", response_model=ChatResponse)
async def handle_chat(request: ChatRequest):
    """自然语言对话接口：由 Ollama 识别意图并自动创建日程/待办"""
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="输入内容不能为空")

    try:
        ai_result = await agent.process_query(request.text, history=request.history)
        result_type = ai_result.get("type")

        # 情况 A: 仅纯文本交流
        if result_type == "text":
            return ChatResponse(
                status="chat",
                message=ai_result.get("reply", "抱歉，我没有理解你的意思。")
            )

        # 情况 B: 触发工具调用，解析参数并提交至 iCloud
        elif result_type == "tool_call":
            item_schema = CalendarItemSchema(**ai_result["args"])
            sync_result = calendar_manager.create_item(item_schema)
            print(f"✅ [日程创建成功] {sync_result}")

            return ChatResponse(
                status="success",
                message=sync_result,
                data=item_schema
            )

        # 情况 C: 模型输出了非 text 与 tool_call 的异常情况（防止幻觉）
        else:
            print(f"Warning: Unexpected AI result type: {result_type}")
            raise HTTPException(status_code=500, detail="模型返回了未知的指令类型，请重试。")

    except ValueError as ve:
        # 捕获 Agent 中抛出的“重试 3 次仍失败”的业务异常
        raise HTTPException(status_code=422, detail=str(ve))
    except Exception as e:
        print(f"\n❌ [执行 iCloud 同步时发生崩溃] 详细堆栈如下:")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"日程创建失败: {str(e)}")

@router.post("/manual-item")
async def create_manual_item(item: CalendarItemSchema):
    """手动创建接口：绕过 AI，直接向 iCloud 写入日程/待办"""
    try:
        sync_result = calendar_manager.create_item(item)
        print(f"✅ [手动创建日程成功] {sync_result}")
        return {"status": "success", "message": sync_result, "data": item}
    except Exception as e:
        print(f"\n❌ [手动创建日程时发生崩溃] 详细堆栈如下:")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"手动创建失败: {str(e)}")

@router.get("/health")
async def health_check():
    """健康检查接口：确认后端与各组件连通性"""
    return {
        "status": "healthy",
        "agent_model": agent.model,
        "timezone": calendar_manager.tz.key
    }
