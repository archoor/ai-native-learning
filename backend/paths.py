"""
运行环境路径解析：统一处理「开发态 / PyInstaller 冻结态」的资源与可写目录。

对外函数：
- is_frozen()        是否处于 PyInstaller 冻结环境
- resource_dir()     只读资源根（开发=ai_native_learning/，冻结=_MEIPASS）
- frontend_dir()     前端静态资源目录
- project_root()     项目根（开发态用于扫描；冻结态回退到 exe 目录）
- user_data_dir()    可写用户数据目录（%APPDATA%/AiNativeLearning 或 ~/.config/AiNativeLearning）
- media_dir()        下载/转码后视频的存放目录
- load_env()         按优先级加载 .env 到环境变量（仅加载一次）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_APP_DIR_NAME = "AiNativeLearning"
_env_loaded = False


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[1]


def frontend_dir() -> Path:
    return resource_dir() / "frontend"


def exe_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return project_root()


def project_root() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def user_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.getenv("APPDATA") or Path.home())
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    d = base / _APP_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def media_dir() -> Path:
    """下载/转码后视频与字幕缓存的存放目录。"""
    d = user_data_dir() / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _env_candidates() -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for p in (exe_dir() / ".env", user_data_dir() / ".env", project_root() / ".env"):
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def load_env() -> None:
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for cand in _env_candidates():
        if cand.exists():
            load_dotenv(cand, override=False)
            break
