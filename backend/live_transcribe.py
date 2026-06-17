"""
流式转录：复用 scripts 既有的转录 + 断句能力，按句产出字幕。

两条后端，按配置自动选择（fun-asr 优先，缺配置时回落 VAD）：

1. fun-asr（推荐，复用 scripts/audio_to_funasr.py）
   - 把媒体按 FUNASR_CHUNK_SECONDS（默认 5 分钟）切片，逐片：
     ffmpeg 切音频 → 上传 OSS → 提交 DashScope fun-asr 异步任务 → 轮询 → 拉结果。
   - fun-asr 返回真实的句级时间戳（begin/end），按 chunk 起点做偏移后，
     用 audio_to_funasr._merge_sentences 做断句（默认 sentence 逐句不合并）。
   - 每片转完即 yield 该片所有字幕 → 实现「每 N 分钟出一批」的近流式体验。
   - 需要 DASHSCOPE_API_KEY + OSS_* 配置齐全。

2. VAD + OpenAI 兼容 STT（回落）
   - RMS VAD 分段（max_group_seconds 较小）→ ffmpeg 切片 → 同步 STT，
     文本再按句末标点拆成字幕级短句、按字符数线性分配时间戳。

不复制任何转录/断句核心逻辑，全部复用 scripts/ 下既有实现。
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Callable, Iterator

# 注入仓库根，确保可 import scripts.*
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.lib import transcript_common as tc  # noqa: E402
from scripts.audio_to_transcript import (  # noqa: E402
    _get_cloud_stt_config,
    _transcribe_with_retry,
)

# ── fun-asr 后端调参 ──────────────────────────────────────────────────────────
FUNASR_CHUNK_SECONDS = 300.0   # 每片提交时长（5 分钟）；越小越早出字幕、任务数越多
FUNASR_GROUPING = "sentence"   # 断句粒度：sentence/short/medium/long（见 audio_to_funasr）
FUNASR_DIARIZATION = False      # 字幕场景默认关闭说话人分离

# ── VAD 回落后端调参 ──────────────────────────────────────────────────────────
MAX_GROUP_SECONDS = 12.0   # VAD 分组上限（越小字幕越细、API 调用越多）
MAX_SUBTITLE_CHARS = 80    # 单行字幕最大字符数，超过则在逗号/分号处再拆

# 句末标点（中英）；用于把一段转写文本切成「字幕级」短句（VAD 路径）
_SENTENCE_END = re.compile(r"(?<=[.!?。！？…])\s+|(?<=[。！？；;])")
_CLAUSE_SPLIT = re.compile(r"(?<=[,，;；、])\s*")


# ── 可用性探测 ────────────────────────────────────────────────────────────────

def funasr_available() -> bool:
    """DASHSCOPE_API_KEY + OSS_* 是否齐全（fun-asr 后端可用）。"""
    if not os.getenv("DASHSCOPE_API_KEY", "").strip():
        return False
    for k in ("OSS_ENDPOINT", "OSS_BUCKET", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET"):
        if not os.getenv(k, "").strip():
            return False
    return True


def cloud_stt_available(model: str | None = None) -> bool:
    """任一 STT 后端可用即可（fun-asr 或 OpenAI 兼容）。"""
    return funasr_available() or (_get_cloud_stt_config(model) is not None)


# ── fun-asr 后端（分片 + 真实句级时间戳） ─────────────────────────────────────

def _build_chunks(duration: float, resume_from: float = 0.0) -> list[tuple[float, float]]:
    """按 FUNASR_CHUNK_SECONDS 把时长切成 [start, end) 片段；跳过 resume_from 之前已完成的片。"""
    if duration <= 0:
        return [(0.0, 0.0)]  # 时长未知：整文件单任务
    chunks: list[tuple[float, float]] = []
    t = 0.0
    while t < duration:
        end = min(duration, t + FUNASR_CHUNK_SECONDS)
        if end > resume_from + 1e-6:
            chunks.append((max(t, resume_from), end))
        t = end
    return chunks


def _transcribe_funasr(
    media: str,
    *,
    language: str | None,
    resume_from: float = 0.0,
    on_total: Callable[[int], None] | None,
    on_progress: Callable[[int, int], None] | None,
    on_commit: Callable[[float], None] | None = None,
) -> Iterator[tuple[float, float, str]]:
    """分片调 fun-asr，逐片 yield (start, end, text)。配置/依赖问题向上抛出以便回落。"""
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

    # 依赖与配置校验放最前：抛出则由调用方回落到 VAD
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
                    upload_path = Path(media)  # 时长未知：直接传原文件

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

                # fun-asr 时间戳是相对该片的，需偏移回全局
                for s in sentences:
                    s.begin_sec += c0
                    s.end_sec += c0

                for seg in _merge_sentences(sentences, thresholds):
                    text = seg.joined_text.strip()
                    if not text:
                        continue
                    # 安全网：fun-asr 偶有超长 run-on 句，按真实时间区间再拆短
                    if len(text) > MAX_SUBTITLE_CHARS:
                        for s2, e2, t2 in split_into_subtitles(
                            text, float(seg.begin_sec), float(seg.end_sec)
                        ):
                            yield s2, e2, t2
                    else:
                        yield float(seg.begin_sec), float(seg.end_sec), text
                # 整片完成 → 提交点（续接游标推进到该片结尾）
                if on_commit is not None and c1 > c0:
                    on_commit(float(c1))
            except Exception as e:  # 单片失败不致命：跳过该片，继续后续
                print(f"[live_transcribe] fun-asr 第 {i + 1}/{total} 片失败，跳过：{e}")
            finally:
                if on_progress is not None:
                    on_progress(i + 1, total)


# ── VAD 回落后端（句末标点拆分 + 线性时间戳） ─────────────────────────────────

def _split_long_clause(sentence: str) -> list[str]:
    """对超过 MAX_SUBTITLE_CHARS 的句子，在逗号/分号处再拆，仍超长则按长度硬切。"""
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
    """把一段转写文本拆成字幕级短句，按字符数比例在 [start, end] 内线性分配时间戳。"""
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


def _transcribe_vad(
    media: str,
    *,
    language: str | None,
    model: str | None,
    resume_from: float = 0.0,
    on_total: Callable[[int], None] | None,
    on_progress: Callable[[int, int], None] | None,
    on_commit: Callable[[float], None] | None = None,
) -> Iterator[tuple[float, float, str]]:
    """VAD 分段 + OpenAI 兼容 STT + 句级拆分。"""
    cfg = _get_cloud_stt_config(model)
    if cfg is None:
        raise RuntimeError(
            "未配置云端 STT（BASE_URL / API_KEY 为空）。请在 .env 中配置后重试。"
        )
    stt_model, base_url, api_key = cfg

    vad_segments = tc.get_vad_segments(media, max_group_seconds=MAX_GROUP_SECONDS)
    if not vad_segments:
        duration = tc.get_duration(media)
        end = duration if duration > 0 else 0.0
        vad_segments = [(0.0, end)]

    # 续接：跳过 resume_from 之前已完成的段
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
                    seg_path, base_url, api_key, stt_model, language,
                    label=f"第 {idx + 1}/{total} 段",
                )
                if text and text.strip():
                    seg_end = end if end > start else start + 5.0
                    for sub_start, sub_end, sub_text in split_into_subtitles(
                        text.strip(), float(start), float(seg_end)
                    ):
                        yield sub_start, sub_end, sub_text
            # 单段完成 → 提交点
            if on_commit is not None and end > start:
                on_commit(float(end))
            if on_progress is not None:
                on_progress(idx + 1, total)


# ── 对外入口 ──────────────────────────────────────────────────────────────────

def transcribe_segments(
    media_path: str | Path,
    *,
    language: str | None = None,
    model: str | None = None,
    resume_from: float = 0.0,
    on_total: Callable[[int], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    on_commit: Callable[[float], None] | None = None,
) -> Iterator[tuple[float, float, str]]:
    """
    对媒体文件流式转录，逐句产出 (start, end, text)。

    优先用 fun-asr（真实句级时间戳 + 分片近流式）；配置缺失或初始化失败时
    回落到 VAD + OpenAI 兼容 STT。

    resume_from：从该媒体秒数之后继续转录（断点续接），之前内容跳过。
    on_total(total)：分段/分片数确定后回调一次（用于进度条）。
    on_progress(done, total)：每处理完一片/段回调一次。
    on_commit(end_sec)：每完成一个可靠单元（fun-asr 一片 / VAD 一段）回调，
        用于推进续接游标（该秒数之前已可靠转录）。
    """
    media = str(media_path)

    if funasr_available():
        gen = _transcribe_funasr(
            media, language=language, resume_from=resume_from,
            on_total=on_total, on_progress=on_progress, on_commit=on_commit,
        )
        try:
            first = next(gen)
        except StopIteration:
            return  # fun-asr 成功但无字幕
        except Exception as e:
            print(f"[live_transcribe] fun-asr 初始化失败，回落到 VAD：{e}")
        else:
            yield first
            yield from gen
            return

    yield from _transcribe_vad(
        media, language=language, model=model, resume_from=resume_from,
        on_total=on_total, on_progress=on_progress, on_commit=on_commit,
    )
