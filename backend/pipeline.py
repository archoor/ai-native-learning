"""
任务编排：来源 → 下载/转码 → 转录与翻译「并行」流式产出，全程向 Job 推事件。

并行机制：
- 转录线程逐句产出 (start,end,text)，每句：追加到 segments.jsonl、推 segment 事件、
  并把该句 index 投入翻译队列。
- 翻译线程从队列取句，攒小批（或空闲超时）调 LLM 批量翻译，每条译文：
  追加到 trans.jsonl、推 translated 事件。
- 两者并行，因此播放过程中字幕（源文 + 译文）可近实时陆续出现。

断点续接：
- 中间结果分离、动态追加存储（store.py：segments.jsonl / trans.jsonl / vtprogress.json）。
- 重新打开同一视频时：
  · 命中完成态整包缓存 .vtjob.json → 直接整包回放；
  · 否则读取增量文件，回放已完成内容，并从 transcribe_cursor 续转、补译未译段。

复用：
- scripts.lib.video_download              下载 + 转 Windows 可播放 mp4
- ai_native_learning.backend.live_transcribe fun-asr/VAD 流式转录（支持 resume_from/on_commit）
- subtitle_player.backend.translator      LLM 批量翻译（_translate_batch + 凭据/参数）
- subtitle_player.backend.parser          源语言判定

事件协议（type）：
  status/ready/lang/segment/translated/progress/error/done
"""

from __future__ import annotations

import queue
import sys
import threading
from pathlib import Path

# 注入仓库根，确保可 import scripts.* 与 subtitle_player.*
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.lib import transcript_common as tc  # noqa: E402
from scripts.lib.transcript_common import _fetch_video_meta, sanitize_filename  # noqa: E402
from scripts.lib.video_download import download_and_transcode, make_playable  # noqa: E402
from subtitle_player.backend import translator  # noqa: E402
from subtitle_player.backend.parser import detect_lang  # noqa: E402

from . import store  # noqa: E402
from .jobs import Job, load_result, save_result  # noqa: E402
from .live_transcribe import transcribe_segments  # noqa: E402
from .paths import media_dir  # noqa: E402

# 翻译攒批：达到该条数或空闲超过 flush 间隔即翻一批（兼顾质量与实时性）
TRANSLATE_BATCH = 8
TRANSLATE_IDLE_FLUSH_S = 1.5
_SENTINEL = object()


def _set_status(job: Job, status: str) -> None:
    job.status = status
    job.emit({"type": "status", "status": status})


def _throttled_progress_emitter(job: Job, phase: str, min_delta: float = 1.0):
    """构造一个按百分比节流的进度回调，避免事件过密。"""
    last = {"pct": -100.0}

    def _emit(pct: float) -> None:
        if pct - last["pct"] >= min_delta or pct >= 100.0:
            last["pct"] = pct
            job.emit({"type": "progress", "phase": phase, "percent": round(pct, 1)})

    return _emit


def _prepare_media(job: Job, out_file: Path) -> Path:
    """按来源类型产出本地可播放 mp4，期间推送下载/转码进度。"""
    if job.source_type == "url":
        def _on_dl(pct: float) -> None:
            if job.status != "downloading":
                _set_status(job, "downloading")
            _throttled_dl(pct)

        def _on_tc(pct: float) -> None:
            if job.status != "transcoding":
                _set_status(job, "transcoding")
            _throttled_tc(pct)

        _throttled_dl = _throttled_progress_emitter(job, "download")
        _throttled_tc = _throttled_progress_emitter(job, "transcode")
        _set_status(job, "downloading")
        return download_and_transcode(
            job.url, media_dir(), output_file=out_file,
            on_download_progress=_on_dl,
            on_transcode_progress=_on_tc,
        )

    # 本地文件 / 上传文件：仅需转封装或转码
    src = Path(job.url)
    if not src.exists():
        raise RuntimeError(f"本地文件不存在：{src}")

    def _on_tc2(pct: float) -> None:
        if job.status != "transcoding":
            _set_status(job, "transcoding")
        _throttled_tc2(pct)

    _throttled_tc2 = _throttled_progress_emitter(job, "transcode")
    _set_status(job, "transcoding")
    make_playable(src, out_file, on_transcode_progress=_on_tc2)
    return out_file


# ── 完成态整包回放 ────────────────────────────────────────────────────────────

def _replay_cached(job: Job, video_path: Path, cached: dict) -> None:
    """命中 .vtjob.json：直接回放完整结果。"""
    job.video_path = str(video_path)
    job.duration = float(cached.get("duration") or tc.get_duration(str(video_path)) or 0.0)
    job.src_lang = cached.get("src_lang") or ""
    job.title = cached.get("title") or ""
    job.segments = list(cached.get("segments") or [])

    job.emit({
        "type": "ready",
        "duration": job.duration,
        "video_url": f"/api/video/{job.id}",
        "title": job.title,
        "cached": True,
    })
    if job.src_lang:
        job.emit({"type": "lang", "src_lang": job.src_lang})
    for seg in job.segments:
        job.emit({
            "type": "segment",
            "index": seg["index"], "start": seg["start"], "end": seg["end"],
            "source": seg.get("source", ""),
        })
        if seg.get("target"):
            job.emit({"type": "translated", "index": seg["index"], "target": seg["target"]})
    _set_status(job, "done")
    job.emit({"type": "done"})
    job.finish()


# ── 翻译线程：从队列攒批翻译，增量落盘 ────────────────────────────────────────

def _translate_worker(
    job: Job,
    q: "queue.Queue",
    state: dict,
) -> None:
    """消费翻译队列：攒批 → LLM 翻译 → 推 translated 事件 + 追加 trans.jsonl。"""
    cfg = translator._get_llm_config()
    if cfg is None:
        job.emit({
            "type": "error",
            "message": "未配置翻译 API。请在设置面板填写 Base URL 与 API Key，或在 .env 中配置。",
        })
        # 仍需排空队列，避免转录线程阻塞
        while q.get() is not _SENTINEL:
            pass
        return

    _model, base_url, api_key = cfg
    params = translator.get_effective_params()
    model = params.model

    batch: list[int] = []

    def _flush() -> None:
        if not batch:
            return
        # 首批前确定源语言（用已攒文本判定更稳）
        if not state.get("src_lang"):
            sample = " ".join(job.segments[i]["source"] for i in batch)
            src_lang = detect_lang(sample) or "en"
            state["src_lang"] = src_lang
            job.src_lang = src_lang
            store.save_progress(job.id, src_lang=src_lang)
            job.emit({"type": "lang", "src_lang": src_lang})

        src_lang = state["src_lang"]
        target_lang = "zh" if src_lang == "en" else "en"
        lines = [job.segments[i]["source"] for i in batch]
        try:
            translated = translator._translate_batch(
                lines, target_lang, model, base_url, api_key,
                temperature=params.temperature, max_tokens=params.max_tokens,
            )
        except Exception as e:  # noqa: BLE001
            job.emit({"type": "error", "message": f"批次翻译失败：{type(e).__name__}: {e}"})
            translated = ["" for _ in lines]

        for i, tr in zip(batch, translated):
            if tr:
                job.segments[i]["target"] = tr
                store.append_translation(job.id, i, tr)
                job.emit({"type": "translated", "index": i, "target": tr})

        done = sum(1 for s in job.segments if s.get("target"))
        job.emit({"type": "progress", "phase": "translate",
                  "done": done, "total": len(job.segments)})
        batch.clear()

    while True:
        try:
            item = q.get(timeout=TRANSLATE_IDLE_FLUSH_S)
        except queue.Empty:
            _flush()  # 空闲：把已攒的小批先翻出来，保证实时性
            continue
        if item is _SENTINEL:
            _flush()
            break
        batch.append(item)
        if len(batch) >= TRANSLATE_BATCH:
            _flush()


# ── 续接：从增量文件恢复已完成内容 ────────────────────────────────────────────

def _resume_from_store(job: Job, video_path: Path, progress: dict) -> float:
    """
    读取增量文件，回放已完成内容，返回转录续接游标（秒）。

    压实策略：只保留 end<=cursor 的可靠转录段（丢弃游标之后的半提交段），
    重排索引并重写两个 .jsonl，确保后续追加索引连续、无重复。
    """
    job.video_path = str(video_path)
    job.duration = float(progress.get("duration") or tc.get_duration(str(video_path)) or 0.0)
    job.title = progress.get("title") or Path(video_path).stem
    job.src_lang = progress.get("src_lang") or ""
    cursor = float(progress.get("transcribe_cursor") or 0.0)
    if progress.get("transcribe_complete"):
        cursor = max(cursor, job.duration)

    raw_segs = store.load_segments(job.id)
    committed = [s for s in raw_segs if s.get("end", 0.0) <= cursor + 1e-6]
    committed.sort(key=lambda s: (s.get("start", 0.0), s.get("end", 0.0)))
    for i, s in enumerate(committed):
        s["index"] = i
        s.setdefault("target", "")
    store.rewrite_segments(job.id, committed)

    trans_map = store.load_translations(job.id)
    kept_trans: list[tuple[int, str]] = []
    for s in committed:
        t = trans_map.get(s["index"], "")
        if t:
            s["target"] = t
            kept_trans.append((s["index"], t))
    store.rewrite_translations(job.id, kept_trans)

    job.segments = committed

    _set_status(job, "ready")
    job.emit({
        "type": "ready",
        "duration": job.duration,
        "video_url": f"/api/video/{job.id}",
        "title": job.title,
        "resumed": True,
    })
    if job.src_lang:
        job.emit({"type": "lang", "src_lang": job.src_lang})
    for s in committed:
        job.emit({
            "type": "segment",
            "index": s["index"], "start": s["start"], "end": s["end"],
            "source": s.get("source", ""),
        })
        if s.get("target"):
            job.emit({"type": "translated", "index": s["index"], "target": s["target"]})
    return cursor


# ── 并行转录 + 翻译 ───────────────────────────────────────────────────────────

def _run_parallel(
    job: Job,
    video_path: Path,
    *,
    resume_from: float,
    transcribe_complete: bool,
) -> None:
    q: "queue.Queue" = queue.Queue()
    state: dict = {"src_lang": job.src_lang or ""}

    worker = threading.Thread(target=_translate_worker, args=(job, q, state), daemon=True)
    worker.start()

    # 续接时：把已恢复但未译的段先入队
    for s in job.segments:
        if not s.get("target"):
            q.put(s["index"])

    if not transcribe_complete:
        _set_status(job, "transcribing")

        def _on_total(total: int) -> None:
            job.emit({"type": "progress", "phase": "transcribe", "done": 0, "total": total})

        def _on_seg_progress(done: int, total: int) -> None:
            job.emit({"type": "progress", "phase": "transcribe", "done": done, "total": total})

        def _on_commit(end_sec: float) -> None:
            store.save_progress(job.id, transcribe_cursor=end_sec)

        for start, end, text in transcribe_segments(
            str(video_path), resume_from=resume_from,
            on_total=_on_total, on_progress=_on_seg_progress, on_commit=_on_commit,
        ):
            index = len(job.segments)
            seg = {"index": index, "start": start, "end": end, "source": text, "target": ""}
            job.segments.append(seg)
            store.append_segment(job.id, seg)
            job.emit({"type": "segment", "index": index, "start": start, "end": end,
                      "source": text})
            q.put(index)

        store.save_progress(job.id, transcribe_complete=True)

    # 结束转录 → 通知翻译线程收尾
    q.put(_SENTINEL)
    if job.segments:
        _set_status(job, "translating")
    worker.join()


def _finalize(job: Job) -> None:
    if not job.src_lang:
        # 极端情况下（全部段为空/未触发翻译）兜底判定
        text = " ".join(s.get("source", "") for s in job.segments)
        job.src_lang = detect_lang(text) if text.strip() else ""
    store.save_progress(job.id, translate_complete=True, src_lang=job.src_lang)
    save_result(job)  # 完成态整包缓存，供下次整包回放
    _set_status(job, "done")
    job.emit({"type": "done"})
    job.finish()


def _derive_title(job: Job) -> str:
    """为可读文件名确定标题：URL 取站点元信息，本地/上传取文件名。"""
    if job.title:
        return job.title
    if job.source_type == "url":
        try:
            title, vid = _fetch_video_meta(job.url)
            if title or vid:
                return title or vid  # type: ignore[return-value]
        except Exception:
            pass
        return "video"
    stem = Path(job.url).stem
    return stem or "video"


def _run(job: Job) -> None:
    try:
        # 按 job_id 解析既有产物（命中缓存 / 续接都依赖文件名 stem 解析）
        out_file = store.media_file(job.id)

        # 1) 完成态整包缓存命中 → 直接回放
        cached = load_result(job.id) if out_file.exists() else None
        if cached:
            _replay_cached(job, out_file, cached)
            return

        progress = store.load_progress(job.id)

        # 2) 已有视频 + 增量进度 → 断点续接
        if out_file.exists() and progress:
            cursor = _resume_from_store(job, out_file, progress)
            transcribe_complete = bool(progress.get("transcribe_complete"))
        else:
            # 3) 全新任务：先确定可读文件名（stem = 标题.job_id），再下载/转码
            job.title = _derive_title(job)
            stem = f"{sanitize_filename(job.title) or 'video'}.{job.id}"
            store.register(
                job.id, stem,
                title=job.title, url=job.url, source_type=job.source_type,
            )
            out_file = store.media_file(job.id)

            video_path = _prepare_media(job, out_file)
            job.video_path = str(video_path)
            job.duration = tc.get_duration(str(video_path)) or 0.0
            store.save_progress(
                job.id,
                url=job.url, source_type=job.source_type, title=job.title,
                duration=job.duration, transcribe_cursor=0.0,
                transcribe_complete=False, translate_complete=False,
            )
            _set_status(job, "ready")
            job.emit({
                "type": "ready",
                "duration": job.duration,
                "video_url": f"/api/video/{job.id}",
                "title": job.title,
            })
            cursor = 0.0
            transcribe_complete = False

        # 4) 并行转录 + 翻译
        _run_parallel(
            job, out_file, resume_from=cursor, transcribe_complete=transcribe_complete,
        )

        # 5) 收尾（无字幕也算完成）
        _finalize(job)

    except Exception as exc:  # noqa: BLE001 - 顶层兜底，错误回传前端
        job.error = f"{type(exc).__name__}: {exc}"
        job.status = "error"
        job.emit({"type": "error", "message": job.error})
        job.finish()


def start_job(job: Job) -> None:
    """启动后台处理线程（幂等：已启动则忽略）。"""
    with job._lock:
        if job._started:
            return
        job._started = True
    threading.Thread(target=_run, args=(job,), daemon=True).start()
