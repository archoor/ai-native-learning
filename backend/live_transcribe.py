"""
流式转录：按面板配置的单一 STT 方案转录，无 .env 兜底、无方案间回退。

方案（model_config.stt.active）：
  whisper — 本地 Whisper HTTP 服务 + VAD 分段
  cloud   — OpenAI 兼容 STT + VAD 分段
  funasr  — DashScope fun-asr + OSS 分片
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Callable, Iterator

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.audio_to_transcript import _transcribe_with_retry  # noqa: E402
from scripts.audio_to_whisper import DEFAULT_WHISPER_MODEL, _whisper_transcribe  # noqa: E402
from scripts.lib import transcript_common as tc  # noqa: E402

from .model_config import ConfigError, SttProfile, funasr_env, require_stt, stt_active_ready  # noqa: E402

FUNASR_CHUNK_SECONDS = 300.0
FUNASR_GROUPING = "sentence"
FUNASR_DIARIZATION = False
MAX_GROUP_SECONDS = 12.0
MAX_SUBTITLE_CHARS = 80

_SENTENCE_END = re.compile(r"(?<=[.!?。！？…])\s+|(?<=[。！？；;])")
_CLAUSE_SPLIT = re.compile(r"(?<=[,，;；、])\s*")


def cloud_stt_available() -> bool:
    """当前选中的转录方案是否已配置完整。"""
    return stt_active_ready()


def _build_chunks(duration: float, resume_from: float = 0.0) -> list[tuple[float, float]]:
    if duration <= 0:
        return [(0.0, 0.0)]
    chunks: list[tuple[float, float]] = []
    t = 0.0
    while t < duration:
        end = min(duration, t + FUNASR_CHUNK_SECONDS)
        if end > resume_from + 1e-6:
            chunks.append((max(t, resume_from), end))
        t = end
    return chunks


def _split_long_clause(sentence: str) -> list[str]:
    if len(sentence) <= MAX_SUBTITLE_CHARS:
        return [sentence]
    clauses = [c.strip() for c in _CLAUSE_SPLIT.split(sentence) if c.strip()]
    out: list[str] = []
    buf = ""
    for c in clauses:
        if not buf:
            buf = c
        elif len(buf) + 1 + len(c) <= MAX_SUBTITLE_CHARS:
            buf = f"{buf} {c}"
        else:
            out.append(buf)
            buf = c
    if buf:
        out.append(buf)
    final: list[str] = []
    for s in out:
        while len(s) > MAX_SUBTITLE_CHARS:
            final.append(s[:MAX_SUBTITLE_CHARS])
            s = s[MAX_SUBTITLE_CHARS:]
        if s:
            final.append(s)
    return final or [sentence]


def split_into_subtitles(
    text: str, start: float, end: float
) -> list[tuple[float, float, str]]:
    text = text.strip()
    if not text:
        return []
    sentences = [s.strip() for s in _SENTENCE_END.split(text) if s and s.strip()]
    pieces: list[str] = []
    for s in sentences:
        pieces.extend(_split_long_clause(s))
    if not pieces:
        pieces = [text]
    total_chars = sum(len(p) for p in pieces) or 1
    span = max(end - start, 0.001)
    out: list[tuple[float, float, str]] = []
    cursor = start
    for p in pieces:
        dur = span * (len(p) / total_chars)
        seg_start = cursor
        seg_end = min(end, cursor + dur)
        out.append((seg_start, seg_end, p))
        cursor = seg_end
    s0, _, p0 = out[-1]
    out[-1] = (s0, end, p0)
    return out


def _text_from_whisper_payload(payload: dict) -> str:
    text = (payload.get("text") or "").strip()
    if text:
        return text
    parts = [(s.get("text") or "").strip() for s in payload.get("segments") or []]
    return " ".join(p for p in parts if p).strip()


def _transcribe_funasr(
    media: str,
    *,
    language: str | None,
    resume_from: float = 0.0,
    on_total: Callable[[int], None] | None,
    on_progress: Callable[[int, int], None] | None,
    on_commit: Callable[[float], None] | None,
) -> Iterator[tuple[float, float, str]]:
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

    import httpx  # noqa: F401
    import oss2  # noqa: F401

    dscfg = DashScopeConfig.from_env()
    ocfg = OSSConfig.from_env()
    _, thresholds = _resolve_grouping(FUNASR_GROUPING)
    language_hints = [language] if language else DEFAULT_LANGUAGE_HINTS

    duration = tc.get_duration(media)
    chunks = _build_chunks(duration, resume_from)
    total = len(chunks)
    if on_total is not None:
        on_total(total)

    with tempfile.TemporaryDirectory(prefix="vt_funasr_") as tmp_dir:
        for i, (c0, c1) in enumerate(chunks):
            try:
                if c1 > c0:
                    seg_path = os.path.join(tmp_dir, f"chunk_{i:03d}.mp3")
                    ok = tc.extract_segment_ffmpeg(media, c0, c1, seg_path)
                    upload_path = Path(seg_path) if ok and os.path.exists(seg_path) else None
                else:
                    upload_path = Path(media)

                if upload_path is None:
                    continue

                signed_url = _upload_to_oss(upload_path, ocfg)
                task_id = _funasr_submit(
                    dscfg, signed_url,
                    model="fun-asr",
                    language_hints=language_hints,
                    diarization=FUNASR_DIARIZATION,
                    speaker_count=DEFAULT_SPEAKER_COUNT,
                )
                payload = _funasr_poll(dscfg, task_id, poll_timeout=DEFAULT_POLL_TIMEOUT_S)
                sentences = _parse_funasr_result(payload)

                for s in sentences:
                    s.begin_sec += c0
                    s.end_sec += c0

                for seg in _merge_sentences(sentences, thresholds):
                    text = seg.joined_text.strip()
                    if not text:
                        continue
                    if len(text) > MAX_SUBTITLE_CHARS:
                        for s2, e2, t2 in split_into_subtitles(
                            text, float(seg.begin_sec), float(seg.end_sec)
                        ):
                            yield s2, e2, t2
                    else:
                        yield float(seg.begin_sec), float(seg.end_sec), text
                if on_commit is not None and c1 > c0:
                    on_commit(float(c1))
            except Exception as e:
                print(f"[live_transcribe] fun-asr 第 {i + 1}/{total} 片失败，跳过：{e}")
            finally:
                if on_progress is not None:
                    on_progress(i + 1, total)


def _transcribe_cloud(
    media: str,
    profile: SttProfile,
    *,
    language: str | None,
    resume_from: float = 0.0,
    on_total: Callable[[int], None] | None,
    on_progress: Callable[[int, int], None] | None,
    on_commit: Callable[[float], None] | None,
) -> Iterator[tuple[float, float, str]]:
    lang = language
    if not lang and profile.language_hint and profile.language_hint != "auto":
        lang = profile.language_hint

    vad_segments = tc.get_vad_segments(media, max_group_seconds=MAX_GROUP_SECONDS)
    if not vad_segments:
        duration = tc.get_duration(media)
        end = duration if duration > 0 else 0.0
        vad_segments = [(0.0, end)]

    if resume_from > 0:
        vad_segments = [
            (max(s, resume_from), e) for (s, e) in vad_segments if e > resume_from + 1e-6
        ]

    total = len(vad_segments)
    if on_total is not None:
        on_total(total)

    with tempfile.TemporaryDirectory(prefix="vt_stt_") as tmp_dir:
        for idx, (start, end) in enumerate(vad_segments):
            seg_path = os.path.join(tmp_dir, f"seg_{idx:05d}.mp3")
            ok = tc.extract_segment_ffmpeg(media, start, end, seg_path)
            if ok and os.path.exists(seg_path):
                text = _transcribe_with_retry(
                    seg_path, profile.base_url, profile.api_key, profile.model, lang,
                    label=f"第 {idx + 1}/{total} 段",
                )
                if text and text.strip():
                    seg_end = end if end > start else start + 5.0
                    for sub_start, sub_end, sub_text in split_into_subtitles(
                        text.strip(), float(start), float(seg_end)
                    ):
                        yield sub_start, sub_end, sub_text
            if on_commit is not None and end > start:
                on_commit(float(end))
            if on_progress is not None:
                on_progress(idx + 1, total)


def _transcribe_whisper(
    media: str,
    profile: SttProfile,
    *,
    language: str | None,
    resume_from: float = 0.0,
    on_total: Callable[[int], None] | None,
    on_progress: Callable[[int, int], None] | None,
    on_commit: Callable[[float], None] | None,
) -> Iterator[tuple[float, float, str]]:
    lang = language
    if not lang and profile.language_hint and profile.language_hint != "auto":
        lang = profile.language_hint
    model = profile.model or DEFAULT_WHISPER_MODEL

    vad_segments = tc.get_vad_segments(media, max_group_seconds=MAX_GROUP_SECONDS)
    if not vad_segments:
        duration = tc.get_duration(media)
        end = duration if duration > 0 else 0.0
        vad_segments = [(0.0, end)]

    if resume_from > 0:
        vad_segments = [
            (max(s, resume_from), e) for (s, e) in vad_segments if e > resume_from + 1e-6
        ]

    total = len(vad_segments)
    if on_total is not None:
        on_total(total)

    with tempfile.TemporaryDirectory(prefix="vt_whisper_") as tmp_dir:
        for idx, (start, end) in enumerate(vad_segments):
            seg_path = os.path.join(tmp_dir, f"seg_{idx:05d}.mp3")
            ok = tc.extract_segment_ffmpeg(media, start, end, seg_path)
            if ok and os.path.exists(seg_path):
                payload = _whisper_transcribe(
                    Path(seg_path),
                    base_url=profile.base_url,
                    model=model,
                    language=lang,
                    timeout=profile.timeout_sec,
                )
                text = _text_from_whisper_payload(payload)
                if text:
                    seg_end = end if end > start else start + 5.0
                    for sub_start, sub_end, sub_text in split_into_subtitles(
                        text, float(start), float(seg_end)
                    ):
                        yield sub_start, sub_end, sub_text
            if on_commit is not None and end > start:
                on_commit(float(end))
            if on_progress is not None:
                on_progress(idx + 1, total)


def transcribe_segments(
    media_path: str | Path,
    *,
    language: str | None = None,
    model: str | None = None,  # noqa: ARG001 — 使用面板配置
    resume_from: float = 0.0,
    on_total: Callable[[int], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    on_commit: Callable[[float], None] | None = None,
) -> Iterator[tuple[float, float, str]]:
    """按面板 active STT 方案转录；配置不足时抛 ConfigError。"""
    profile = require_stt()

    if profile.backend == "funasr":
        with funasr_env(profile):
            yield from _transcribe_funasr(
                str(media_path), language=language, resume_from=resume_from,
                on_total=on_total, on_progress=on_progress, on_commit=on_commit,
            )
        return

    if profile.backend == "cloud":
        yield from _transcribe_cloud(
            str(media_path), profile, language=language, resume_from=resume_from,
            on_total=on_total, on_progress=on_progress, on_commit=on_commit,
        )
        return

    yield from _transcribe_whisper(
        str(media_path), profile, language=language, resume_from=resume_from,
        on_total=on_total, on_progress=on_progress, on_commit=on_commit,
    )
