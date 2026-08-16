"""单卡 GPU 的全局推理锁。

项目在单张 8GB 显卡上同时跑两个本地模型：
  - qwen2.5:7b（文本作业识别 + 主系统对话）
  - qwen2.5vl:7b（图片 OCR）
两个模型都常驻显存时本就放不下，若再并发调用 Ollama，GPU 会在二者之间
反复 swap / offload 到 CPU，造成 37s 级卡顿。

因此所有指向 Ollama 的推理（文本检测、图片 OCR、主系统 /api/chat）都先抢
这把锁，保证同一时刻只有一次推理在跑 —— 一个在跑，另一个就 pending。
"""
import asyncio

inference_lock = asyncio.Lock()
