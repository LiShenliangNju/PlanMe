"""白名单配置读写接口：前端「配置 / 白名单」页的数据源。

读取 / 写回 .config/hmwk_scnr/config.yaml 中的两个群白名单：
- qq.group_whitelist   ：作业扫描群（且限定老师身份）
- image.group_whitelist：图片 OCR 群（群内所有图片都 OCR 存档）

写回采用「定点改写」：直接基于原始文本，只替换目标 section 下 `group_whitelist:`
那一行及其列表项，其余原文（含所有注释、其他字段、缩进风格）一字不动。
不依赖 ruamel.yaml，因此 YAML 注释会被完整保留。
修改后需重启 homework 扫描器才会生效。
"""
from pathlib import Path

import yaml
from fastapi import APIRouter
from pydantic import BaseModel

from settings import settings

router = APIRouter(prefix="/api/config", tags=["Config"])

_CONFIG_PATH = Path(settings.HMWK_SCRN_CONFIG_PATH)


class WhitelistPayload(BaseModel):
    homework_groups: list[int] | None = None
    image_groups: list[int] | None = None


def _read() -> dict:
    return yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}


def _replace_section_whitelist(text: str, section: str, new_ids: list[int]) -> str:
    """在 text 中找到顶层 `section:` 块，将其下的 `group_whitelist:` 行定点替换为新列表。

    仅替换命中 section 的那一处；其余行（含注释、其他字段）原样保留。
    空列表写成 `[]` 行内形式；非空写成 block 列表（更易读、可编辑）。
    """
    lines = text.split("\n")
    out: list[str] = []
    cur_section: str | None = None
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        # 顶层 key 检测：行首无缩进且以 ':' 结尾
        if line and not line[0].isspace() and line.strip().endswith(":"):
            cur_section = line.strip()[:-1]
            out.append(line)
            i += 1
            continue
        if cur_section == section and line.lstrip().startswith("group_whitelist:"):
            indent = len(line) - len(line.lstrip())
            key_only = line.lstrip().split(":", 1)[0]  # "group_whitelist"
            # 跳过旧的列表项（缩进更深的 '- ' 行），直到空行 / 浅缩进 / 新 key
            j = i + 1
            while j < n:
                nxt = lines[j]
                if nxt.strip() == "":
                    break
                if (len(nxt) - len(nxt.lstrip())) > indent and nxt.lstrip().startswith("-"):
                    j += 1
                    continue
                break
            if new_ids:
                out.append(" " * indent + key_only + ":")
                for gid in new_ids:
                    out.append(" " * (indent + 2) + f"- {gid}")
            else:
                out.append(" " * indent + key_only + ": []")
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


@router.get("/whitelist")
async def get_whitelist():
    cfg = _read()
    return {
        "homework_groups": (cfg.get("qq") or {}).get("group_whitelist", []),
        "image_groups": (cfg.get("image") or {}).get("group_whitelist", []),
    }


@router.post("/whitelist")
async def update_whitelist(payload: WhitelistPayload):
    # 基于原始文本定点改写，保留其余全部注释与字段
    text = _CONFIG_PATH.read_text(encoding="utf-8")
    if payload.homework_groups is not None:
        text = _replace_section_whitelist(text, "qq", [int(x) for x in payload.homework_groups])
    if payload.image_groups is not None:
        text = _replace_section_whitelist(text, "image", [int(x) for x in payload.image_groups])
    _CONFIG_PATH.write_text(text, encoding="utf-8")
    return {"status": "success", "message": "白名单已更新，重启扫描器后生效"}
