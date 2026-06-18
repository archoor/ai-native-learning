"""语音转写：使用面板配置的单一 STT 方案，无回退。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.audio_to_transcript import _transcribe_single_cloud  # noqa: E402
from scripts.audio_to_whisper import DEFAULT_WHISPER_MODEL, _whisper_transcribe  # noqa: E402

from ..model_config import (  # noqa: E402
    STT_LABELS,
    funasr_env,
    readiness,
    require_stt,
    stt_active_ready,
)

_health_cache: dict[str, tuple[float, bool, str]] = {}
_CACHE_TTL = 30.0


def _text_from_whisper_payload(payload: dict) -> str:
    text = (payload.get("text") or "").strip()
    if text:
        return text
    parts = [(s.get("text") or "").strip() for s in payload.get("segments") or []]
    return " ".join(p for p in parts if p).strip()


def _whisper_health(base_url: str) -> tuple[bool, str]:
    if not base_url:
        return False, "未配置服务 URL"
    now = time.time()
    cached = _health_cache.get(base_url)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1], cached[2]
    try:
        import httpx
        with httpx.Client(timeout=5.0, trust_env=False) as cli:
            resp = cli.get(f"{base_url.rstrip('/')}/health")
            resp.raise_for_status()
            info = resp.json()
        if info.get("status") != "ok":
            ok, model = False, "服务未就绪"
        else:
            ok, model = True, info.get("model") or DEFAULT_WHISPER_MODEL
    except Exception:
        ok, model = False, "无法连接"
    _health_cache[base_url] = (now, ok, model)
    return ok, model


def voice_status() -> dict:
    r = readiness()
    active = r.get("stt_active") or ""
    return {
        "available": r.get("stt_ready", False),
        "active": active,
        "active_label": STT_LABELS.get(active, ""),
        "backends": r.get("stt_backends") or {},
        "backend": active or "none",
    }


def transcribe_file(path: str | Path, *, language: str | None = "zh") -> tuple[str, str]:
    """转写短音频；返回 (text, backend)。"""
    profile = require_stt()
    p = Path(path)

    if profile.backend == "whisper":
        payload = _whisper_transcribe(
            p,
            base_url=profile.base_url,
            model=profile.model or DEFAULT_WHISPER_MODEL,
            language=language,
            timeout=profile.timeout_sec,
        )
        text = _text_from_whisper_payload(payload)
        if not text:
            raise RuntimeError("Whisper 返回空文本")
        return text, "whisper"

    if profile.backend == "cloud":
        lang = language
        if not lang and profile.language_hint and profile.language_hint != "auto":
            lang = profile.language_hint
        text = _transcribe_single_cloud(
            str(p), profile.base_url, profile.api_key, profile.model, language=lang
        )
        return text, "cloud"

    with funasr_env(profile):
        from scripts.audio_to_funasr import (  # noqa: E402
            DEFAULT_LANGUAGE_HINTS,
            DEFAULT_POLL_TIMEOUT_S,
            DEFAULT_SPEAKER_COUNT,
            DashScopeConfig,
            OSSConfig,
            _funasr_poll,
            _funasr_submit,
            _merge_sentences,
            _parse_funasr_result,
            _resolve_grouping,
            _upload_to_oss,
        )
        dscfg = DashScopeConfig.from_env()
        ocfg = OSSConfig.from_env()
        _, thresholds = _resolve_grouping("sentence")
        hints = [language] if language else DEFAULT_LANGUAGE_HINTS
        signed_url = _upload_to_oss(p, ocfg)
        task_id = _funasr_submit(
            dscfg, signed_url, model="fun-asr",
            language_hints=hints, diarization=False, speaker_count=DEFAULT_SPEAKER_COUNT,
        )
        payload = _funasr_poll(dscfg, task_id, poll_timeout=DEFAULT_POLL_TIMEOUT_S)
        sentences = _parse_funasr_result(payload)
        parts = [seg.joined_text.strip() for seg in _merge_sentences(sentences, thresholds)]
        text = " ".join(x for x in parts if x).strip()
        if not text:
            raise RuntimeError("Fun-ASR 返回空文本")
        return text, "funasr"
