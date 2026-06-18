"""最近打开视频历史：持久化到 user_data_dir/recent_videos.json，最多 10 条。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .paths import user_data_dir

HISTORY_MAX = 10
_lock = threading.Lock()


def _path() -> Path:
    return user_data_dir() / "recent_videos.json"


def _load_raw() -> list[dict[str, Any]]:
    p = _path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        return [x for x in data if isinstance(x, dict) and x.get("source")]
    except Exception:
        return []


def _save_raw(items: list[dict[str, Any]]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 opened_at 倒序，去重 source，截断至 HISTORY_MAX。"""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for it in sorted(items, key=lambda x: int(x.get("opened_at") or 0), reverse=True):
        src = str(it.get("source") or "").strip()
        if not src or src in seen:
            continue
        seen.add(src)
        out.append({
            "source": src,
            "name": str(it.get("name") or src),
            "opened_at": int(it.get("opened_at") or 0),
        })
        if len(out) >= HISTORY_MAX:
            break
    return out


def list_history() -> list[dict[str, Any]]:
    with _lock:
        return _normalize(_load_raw())


def push_history(source: str, name: str = "") -> list[dict[str, Any]]:
    src = (source or "").strip()
    if not src:
        return list_history()
    now = int(time.time() * 1000)
    with _lock:
        items = [x for x in _load_raw() if x.get("source") != src]
        items.append({"source": src, "name": name or src, "opened_at": now})
        normalized = _normalize(items)
        _save_raw(normalized)
        return normalized


def remove_history(source: str) -> list[dict[str, Any]]:
    src = (source or "").strip()
    with _lock:
        items = [x for x in _load_raw() if x.get("source") != src]
        normalized = _normalize(items)
        _save_raw(normalized)
        return normalized
