import sys
from pathlib import Path

# 1. 注册 .config 目录路径，保证 settings 可被正常引用
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR / ".config"))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.routes import router as api_router

app = FastAPI(
    title="Planme AI Agent API",
    description="基于 Ollama + CalDAV 的智能日程管理服务",
    version="1.0.0"
)

# 2. 配置 CORS 跨域权限
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. 挂载 API 路由
app.include_router(api_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)