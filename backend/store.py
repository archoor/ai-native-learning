"""
中间结果的分离式增量存储，支持断点续接。

每个任务（按 job.id）在 media_dir 下维护三个文件，与 {job.id}.mp4 同目录：

  {job.id}.segments.jsonl   转录结果，逐行追加：{"index","start","end","source"}
  {job.id}.trans.jsonl      翻译结果，逐行追加：{"index","target"}
  {job.id}.vtprogress.json  进度/元信息（小 JSON，覆盖写）：
        {url, source_type, title, duration, src_lang,
         transcribe_cursor(已可靠转录到的媒体秒数),
         transcribe_complete, translate_complete}

设计要点：
- 两条 .jsonl 由不同线程分别追加（转录线程写 segments、翻译线程写 trans），
  互不竞争；progress.json 的更新用锁串行化。
- transcribe_cursor 是「提交点」：转录线程每完成一个可靠单元（fun-asr 一片/
  VAD 一段）才推进。续接时据此跳过已完成内容，并丢弃游标之后可能残留的半提交段。
- 任务全部完成后由 pipeline 另写 {job.id}.vtjob.json 作为「完成态」整包缓存，
  下次打开优先整包回放；未完成则走这里的增量文件做续接。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .paths import media_dir

_progress_lock = threading.Lock()

# ── 文件名解析 ────────────────────────────────────────────────────────────────
# 产物统一以「可读 stem」命名，stem = "{安全标题}.{job_id}"，便于在 media 目录中辨别；
# job_id（16 位 hex）始终嵌在文件名中，按它即可稳定定位同一任务的全部产物：
#   {stem}.mp4 / {stem}.vtjob.json / {stem}.segments.jsonl /
#   {stem}.trans.jsonl / {stem}.vtprogress.json
# media/index.json 维护 {job_id: {stem,title,url,source_type}} 以便快速解析与浏览。

_EXTS = (".mp4", ".vtjob.json", ".segments.jsonl", ".trans.jsonl", ".vtprogress.json")
_stem_cache: dict[str, str] = {}
_index_lock = threading.Lock()


def _index_path() -> Path:
    return media_dir() / "index.json"


def _load_index() -> dict[str, Any]:
    p = _index_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def register(job_id: str, stem: str, **meta: Any) -> None:
    """登记任务的可读文件名（stem）与元信息，供解析与浏览。"""
    with _index_lock:
        idx = _load_index()
        entry = idx.get(job_id) or {}
        entry["stem"] = stem
        entry.update(meta)
        idx[job_id] = entry
        _index_path().write_text(
            json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    _stem_cache[job_id] = stem


def _resolve_stem(job_id: str) -> str:
    """解析任务的文件名 stem：缓存 → 索引 → 扫描既有产物 → 旧式 {job_id} 兜底。"""
    cached = _stem_cache.get(job_id)
    if cached:
        return cached

    entry = _load_index().get(job_id)
    if entry and entry.get("stem"):
        _stem_cache[job_id] = entry["stem"]
        return entry["stem"]

    d = media_dir()
    for ext in _EXTS:
        for f in d.glob(f"*.{job_id}{ext}"):
            stem = f.name[: -len(ext)]  # → "{title}.{job_id}"
            _stem_cache[job_id] = stem
            return stem

    # 旧式（按 job_id 命名）产物：保持可读取，命中既有缓存
    for ext in _EXTS:
        if (d / f"{job_id}{ext}").exists():
            _stem_cache[job_id] = job_id
            return job_id

    return job_id  # 全新任务的默认 stem（pipeline 随后会 register 为可读名）


def _p(job_id: str, ext: str) -> Path:
    return Path(str(media_dir() / _resolve_stem(job_id)) + ext)


def media_file(job_id: str) -> Path:
    return _p(job_id, ".mp4")


def vtjob_path(job_id: str) -> Path:
    return _p(job_id, ".vtjob.json")


def segments_path(job_id: str) -> Path:
    return _p(job_id, ".segments.jsonl")


def trans_path(job_id: str) -> Path:
    return _p(job_id, ".trans.jsonl")


def progress_path(job_id: str) -> Path:
    return _p(job_id, ".vtprogress.json")


# ── 转录段（segments.jsonl）──────────────────────────────────────────────────

def append_segment(job_id: str, seg: dict[str, Any]) -> None:
    rec = {
        "index": seg["index"],
        "start": seg["start"],
        "end": seg["end"],
        "source": seg.get("source", ""),
    }
    with segments_path(job_id).open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_segments(job_id: str) -> list[dict[str, Any]]:
    p = segments_path(job_id)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def rewrite_segments(job_id: str, segs: list[dict[str, Any]]) -> None:
    """整文件重写（续接时压实，剔除游标之后的半提交段并重排索引）。"""
    lines = [
        json.dumps(
            {"index": s["index"], "start": s["start"], "end": s["end"],
             "source": s.get("source", "")},
            ensure_ascii=False,
        )
        for s in segs
    ]
    segments_path(job_id).write_text(
        ("\n".join(lines) + "\n") if lines else "", encoding="utf-8"
    )


# ── 翻译段（trans.jsonl）────────────────────────────────────────────────────

def append_translation(job_id: str, index: int, target: str) -> None:
    with trans_path(job_id).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"index": index, "target": target}, ensure_ascii=False) + "\n")


def load_translations(job_id: str) -> dict[int, str]:
    p = trans_path(job_id)
    if not p.exists():
        return {}
    out: dict[int, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if rec.get("target"):
                out[int(rec["index"])] = rec["target"]
        except Exception:
            continue
    return out


def rewrite_translations(job_id: str, items: list[tuple[int, str]]) -> None:
    lines = [
        json.dumps({"index": i, "target": t}, ensure_ascii=False)
        for i, t in items
    ]
    trans_path(job_id).write_text(
        ("\n".join(lines) + "\n") if lines else "", encoding="utf-8"
    )


# ── 进度/元信息（vtprogress.json）────────────────────────────────────────────

def load_progress(job_id: str) -> dict[str, Any] | None:
    p = progress_path(job_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_progress(job_id: str, **fields: Any) -> None:
    """合并更新进度文件（已有字段保留，传入字段覆盖）。"""
    with _progress_lock:
        cur = load_progress(job_id) or {}
        cur.update(fields)
        progress_path(job_id).write_text(
            json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def clear(job_id: str) -> None:
    """删除全部增量中间文件（用于完成后清理或重置）。"""
    for p in (segments_path(job_id), trans_path(job_id), progress_path(job_id)):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
