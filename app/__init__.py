"""Planme 应用装配层入口。

统一把项目根下的 .config 注入 sys.path，使 `from settings import settings`
在各模块中始终可用（替代原先散落在 main.py / core/* 的重复 sys.path.insert）。
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
_CONFIG_DIR = str(BASE_DIR / ".config")
if _CONFIG_DIR not in sys.path:
    sys.path.insert(0, _CONFIG_DIR)
