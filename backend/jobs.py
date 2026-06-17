"""
Job 注册表与状态：每个视频 URL 一个处理任务。

一个 Job 经历的阶段（status）：
  queued → downloading → ready（视频已可播放）→ transcribing → translating → done
  出错时 status=error，并带 error 文案。

事件通过 per-job 的线程安全队列向 SSE 推送，前端据此实时更新进度与字幕。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse


def job_id_for(key: str) -> str:
    """按来源标识生成稳定 job_id（同一来源复用结果）。"""
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()[:16]


_YT_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be",
}
# 仅影响来源、不改变「指向哪个视频」的追踪 / 跳转参数：去掉后同一视频归并
_DROP_QUERY = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "spm_id_from", "spm", "feature", "si",
    "ref", "ref_src", "ref_url", "pp", "t", "start_radio", "ab_channel",
}


def canonical_url_key(url: str) -> str:
    """
    把同一视频的不同 URL 形态归并为同一身份键，避免重复下载。

    - YouTube：watch?v=、youtu.be/、shorts/、embed/ → 统一为 "youtube:<id>"
    - Bilibili：BV 号 / av 号 → "bilibili:<id>"
    - 其它：去 fragment、剔除常见追踪参数、规范化 scheme/host/path 后排序拼回。
    解析失败时回退为原始链接（trim 后）。
    """
    u = url.strip()
    try:
        p = urlparse(u)
    except Exception:
        return u
    host = (p.hostname or "").lower()

    if host in _YT_HOSTS:
        vid = None
        if host == "youtu.be":
            vid = p.path.lstrip("/").split("/")[0] or None
        else:
            qs = parse_qs(p.query)
            if qs.get("v"):
                vid = qs["v"][0]
            else:
                m = re.search(r"/(?:shorts|embed|v|live)/([^/?#]+)", p.path)
                if m:
                    vid = m.group(1)
        if vid:
            return f"youtube:{vid}"

    if host.endswith("bilibili.com"):
        m = re.search(r"(BV[0-9A-Za-z]+)", p.path)
        if m:
            return f"bilibili:{m.group(1)}"
        m = re.search(r"/video/av(\d+)", p.path)
        if m:
            return f"bilibili:av{m.group(1)}"

    qs = parse_qs(p.query, keep_blank_values=True)
    kept = {k: v for k, v in qs.items() if k.lower() not in _DROP_QUERY}
    query = urlencode(sorted((k, vv) for k, vs in kept.items() for vv in vs))
    scheme = (p.scheme or "https").lower()
    path = p.path.rstrip("/") or "/"
    return f"{scheme}://{host}{path}" + (f"?{query}" if query else "")


def file_fingerprint(path: str | Path, chunk: int = 1 << 20) -> str:
    """
    本地/上传文件的内容指纹（整文件 sha256 十六进制）。

    用作任务身份键：同一段视频无论从哪个路径打开、还是经上传拷贝进来，
    指纹一致 → job_id 一致 → 复用既有下载/转录/翻译结果，避免重复计算。
    与 upload 流程中对文件字节做的 sha256 完全一致，从而上传与「选择本地文件」互通。
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class Job:
    id: str
    url: str                      # 来源标识：URL / 本地路径 / 上传文件路径
    source_type: str = "url"      # 'url' | 'local' | 'upload'
    status: str = "queued"
    error: str = ""
    video_path: str = ""          # 本地可播放 mp4 绝对路径
    duration: float = 0.0
    src_lang: str = ""            # 'en' | 'zh'
    title: str = ""
    # 字幕段：[{index,start,end,source,target}]
    segments: list[dict[str, Any]] = field(default_factory=list)
    # 全部事件按序留存：SSE 用游标读取，支持断线重连与多端订阅
    history: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _started: bool = False
    _finished: bool = False

    def emit(self, event: dict[str, Any]) -> None:
        """追加一个事件到历史，供 SSE 拉取。"""
        with self._lock:
            self.history.append(event)

    def finish(self) -> None:
        self._finished = True

    def read_since(self, cursor: int) -> tuple[list[dict[str, Any]], int, bool]:
        """返回 (新事件, 新游标, 是否已结束)。"""
        with self._lock:
            new = self.history[cursor:]
            return new, len(self.history), self._finished

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "url": self.url,
                "source_type": self.source_type,
                "status": self.status,
                "error": self.error,
                "duration": self.duration,
                "src_lang": self.src_lang,
                "title": self.title,
                "video_url": f"/api/video/{self.id}" if self.video_path else "",
                "segments": list(self.segments),
            }


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def get_or_create(
        self, key: str, source: str, source_type: str = "url"
    ) -> tuple[Job, bool]:
        """
        返回 (job, created)。created=False 表示命中已有任务。

        key    任务身份键（URL=链接本身；本地/上传=文件内容指纹），决定 job_id 与缓存复用。
        source 实际可处理的来源（URL 或本地文件绝对路径），供下载/转码定位。
        """
        jid = job_id_for(key)
        with self._lock:
            existing = self._jobs.get(jid)
            if existing is not None:
                return existing, False
            job = Job(id=jid, url=source, source_type=source_type)
            self._jobs[jid] = job
            return job, True


registry = JobRegistry()


# ── 结果持久化（用于同一来源复用，免重复下载/转录/翻译）──────────────────────

def save_result(job: Job) -> None:
    if not job.video_path:
        return
    from . import store

    data = {
        "url": job.url,
        "video_file": Path(job.video_path).name,
        "duration": job.duration,
        "src_lang": job.src_lang,
        "title": job.title,
        "segments": job.segments,
    }
    store.vtjob_path(job.id).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_result(job_id: str) -> dict[str, Any] | None:
    from . import store

    cp = store.vtjob_path(job_id)
    if not cp.exists():
        return None
    try:
        return json.loads(cp.read_text(encoding="utf-8"))
    except Exception:
        return None
