"""qwen2.5vl 视觉模型 OCR 端到端测试。

验证 core/homework/ocr.py 的 ImageOCR 能否真正连上本地 Ollama、
对图片做多模态推理并返回 Markdown 文本；并打印 OCR 解析耗时。

用法：
    python test/qwenvl_ocr.py                 # 无参：自动用 Pillow 生成一张带文字的测试图
    python test/qwenvl_ocr.py <图片路径>       # 对指定图片做 OCR
    MODEL=qwen2.5vl:7b OLLAMA_HOST=http://localhost:11434 python test/qwenvl_ocr.py img.png

前置：
    - Ollama 已在本地运行（默认 http://localhost:11434）
    - 已拉取视觉模型：ollama pull qwen2.5vl:7b
依赖：
    - ollama（必选）
    - Pillow（可选，仅无参自动生成测试图时需要）
"""
import asyncio
import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / ".config"))

from ollama import Client

from core.homework.ocr import ImageOCR, _OCR_SYSTEM_PROMPT

MODEL = os.environ.get("QWENVL_MODEL", "qwen2.5vl:7b")
HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


def check_service() -> Client:
    """探活 Ollama 并确认目标视觉模型已拉取；失败则非零退出。"""
    client = Client(host=HOST)
    try:
        resp = client.list()
    except Exception as exc:
        print(f"❌ 无法连接 Ollama（{HOST}）：{exc}")
        sys.exit(1)
    # ollama SDK 不同版本返回结构不同，统一兼容：
    #  - 新版（0.5+）：ListResponse 对象，模型列表在 .models，每个元素有 .model 属性
    #  - 旧版 / 原始 dict：{"models": [{"name"/"model": ...}]}
    if hasattr(resp, "models"):
        names = [m.model for m in resp.models if getattr(m, "model", None)]
    elif isinstance(resp, dict):
        raw = resp.get("models", []) or []
        names = [m.get("name") or m.get("model") for m in raw if isinstance(m, dict)]
    else:
        names = []
    if MODEL not in names:
        print(f"❌ 未找到模型 {MODEL}。请先执行：ollama pull {MODEL}")
        print(f"   已安装模型：{names}")
        sys.exit(1)
    print(f"✅ Ollama 连接正常，模型 {MODEL} 已就绪")
    return client


def resolve_image() -> str:
    """返回待测图片路径：优先用命令行参数，否则用 Pillow 自动生成测试图。"""
    if len(sys.argv) > 1:
        p = sys.argv[1]
        if not Path(p).exists():
            print(f"❌ 图片不存在：{p}")
            sys.exit(1)
        return p

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("❌ 未提供图片路径，且未安装 Pillow 无法自动生成测试图。")
        print("   用法：python test/qwenvl_ocr.py <图片路径>")
        print("   或：pip install pillow 后无参运行。")
        sys.exit(1)

    img = Image.new("RGB", (480, 200), "white")
    d = ImageDraw.Draw(img)
    d.text((20, 20), "PlanMe 视觉测试", fill="black")
    d.text((20, 60), "- 科目：高等数学", fill="black")
    d.text((20, 90), "1. 截止：2026-09-01", fill="black")
    d.text((20, 120), "讲座时间：周三 14:00", fill="black")
    out = str(BASE_DIR / "test" / "_qwenvl_sample.png")
    img.save(out)
    print(f"🖼️ 已生成测试图：{out}")
    return out


async def main() -> None:
    check_service()
    image_path = resolve_image()

    print(f"📝 OCR 系统提示词（取自 ocr._OCR_SYSTEM_PROMPT，{len(_OCR_SYSTEM_PROMPT)} 字）：")
    print(f"   {_OCR_SYSTEM_PROMPT[:80].replace(chr(10), ' ')}…")

    ocr = ImageOCR(host=HOST, model=MODEL, temperature=0.0, throttle_seconds=0)
    print(f"🔍 正在对 {image_path} 做 OCR（模型 {MODEL}）…")

    # OCR 解析计时
    t0 = time.perf_counter()
    md = await ocr.ocr(image_path)
    elapsed = time.perf_counter() - t0
    print(f"⏱️ OCR 解析耗时：{elapsed:.2f}s")

    print("\n========== OCR 结果（Markdown）==========")
    print(md or "（空输出）")
    print("==========================================")

    if md.strip():
        print("✅ OCR 返回非空，视觉模型可用。")
    else:
        print("⚠️ OCR 返回为空，请检查模型 / 图片 / Ollama 日志。")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
