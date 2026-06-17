"""语音转写：优先本地 Whisper，降级云端 STT。"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.audio_to_transcript import (  # noqa: E402
    _get_cloud_stt_config,
    _transcribe_single_cloud,
)
from scripts.audio_to_whisper import (  # noqa: E402
    DEFAULT_WHISPER_BASE_URL,
    DEFAULT_WHISPER_MODEL,
    _whisper_base_url,
    _whisper_transcribe,
)

_health_cache: dict[str, tuple[float, bool, str]] = {}
_CACHE_TTL = 30.0


def _whisper_health() -> tuple[bool, str]:
    """(可用, 模型名或错误简述)"""
    base = _whisper_base_url()
    if not base:
        return False, "未配置 WHISPER_BASE_URL"
    now = time.time()
    cached = _health_cache.get(base)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1], cached[2]
    try:
        import httpx

        with httpx.Client(timeout=5.0, trust_env=False) as cli:
            resp = cli.get(f"{base}/health")
            resp.raise_for_status()
            info = resp.json()
        if info.get("status") != "ok":
            ok, model = False, "服务未就绪"
        else:
            ok, model = True, info.get("model") or DEFAULT_WHISPER_MODEL
    except Exception:
        ok, model = False, "无法连接"
    _health_cache[base] = (now, ok, model)
    return ok, model


def cloud_available() -> bool:
    return _get_cloud_stt_config() is not None


def voice_status() -> dict:
    w_ok, w_model = _whisper_health()
    c_ok = cloud_available()
    backend = "whisper" if w_ok else ("cloud" if c_ok else "none")
    return {
        "available": w_ok or c_ok,
        "whisper": w_ok,
        "whisper_model": w_model if w_ok else "",
        "whisper_url": _whisper_base_url() or DEFAULT_WHISPER_BASE_URL,
        "cloud": c_ok,
        "backend": backend,
    }


def _text_from_whisper_payload(payload: dict) -> str:
    text = (payload.get("text") or "").strip()
    if text:
        return text
    parts = [(s.get("text") or "").strip() for s in payload.get("segments") or []]
    return " ".join(p for p in parts if p).strip()


def transcribe_file(path: str | Path, *, language: str | None = "zh") -> tuple[str, str]:
    """
    转写短音频。返回 (text, backend)。
    backend: whisper | cloud
    """
    p = Path(path)
    w_ok, _ = _whisper_health()
    if w_ok:
        base = _whisper_base_url()
        model = os.getenv("WHISPER_MODEL", "").strip() or DEFAULT_WHISPER_MODEL
        try:
            payload = _whisper_transcribe(
                p,
                base_url=base,
                model=model,
                language=language,
                timeout=120,
            )
            text = _text_from_whisper_payload(payload)
            if text:
                return text, "whisper"
        except Exception:
            pass  # 降级云端

    cfg = _get_cloud_stt_config()
    if cfg is None:
        raise RuntimeError("转写模型不可用")
    model, base_url, api_key = cfg
    text = _transcribe_single_cloud(str(p), base_url, api_key, model, language=language)
    return text, "cloud"
