"""
FastAPI 应用：托管前端 + AI原生学习流水线接口。

路由：
  GET  /                       前端页面
  GET  /api/health             探活 + LLM/STT 是否可用
  POST /api/job                提交视频 URL，返回 job 快照（含 video_url）
  GET  /api/job/{id}           读取 job 当前快照
  GET  /api/job/{id}/events    SSE：阶段 / 视频就绪 / 转录段 / 译文 / 完成
  GET  /api/video/{id}         以 HTTP Range 服务本地 mp4（供 <video> 拖动 seek）
  GET  /api/models-config         读取模型配置
  PUT  /api/models-config         保存模型配置
  POST /api/models-config/reset   清空配置
  POST /api/models-config/test/text  测试文本模型
  POST /api/models-config/test/stt   测试转录模型
  POST /api/models-config/test/proxy 测试备用下载代理
  GET  /api/history            最近打开的视频（最多 10 条）
  POST /api/history            记录打开视频
  POST /api/history/remove     从历史中移除
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# 注入仓库根，确保可 import scripts.* 与 subtitle_player.*
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI, File, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from . import history, jobs, model_config, pipeline  # noqa: E402
from .learning.routes import router as learn_router  # noqa: E402
from .live_transcribe import cloud_stt_available  # noqa: E402
from .paths import frontend_dir, media_dir  # noqa: E402

_FRONTEND = frontend_dir()
# 可处理的本地视频/音频扩展名
_MEDIA_EXTS = {
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".m4v", ".ts", ".wmv",
    ".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac", ".opus",
}

app = FastAPI(title="AI原生学习")
app.include_router(learn_router)


class JobBody(BaseModel):
    # 同时接受 source / url 两个字段名，兼容旧前端缓存
    source: str = Field("", description="视频 URL 或本地文件路径")
    url: str = Field("", description="兼容字段：视频 URL")

    def resolved(self) -> str:
        return (self.source or self.url).strip().strip('"').strip("'")


class ModelsConfigBody(BaseModel):
    text: dict[str, Any] | None = None
    stt: dict[str, Any] | None = None
    download_proxy: dict[str, Any] | None = None


class TestTextBody(BaseModel):
    task: str = Field("translate", description="任务 key")


class TestSttBody(BaseModel):
    backend: str = Field("", description="whisper|cloud|funasr，空则测当前使用")
    stt: dict[str, Any] | None = None


class TestProxyBody(BaseModel):
    download_proxy: dict[str, Any] | None = None


class HistoryBody(BaseModel):
    source: str = Field(..., min_length=1)
    name: str = Field("")


@app.get("/api/health")
def health() -> dict:
    r = model_config.readiness()
    return {
        "ok": True,
        "llm": r.get("translate_ready", False),
        "learn_llm": r.get("learn_ready", False),
        "stt": r.get("stt_ready", False),
        "readiness": r,
    }


def _stt_guard() -> JSONResponse | None:
    if not cloud_stt_available():
        return JSONResponse(
            {"error": "未配置转录模型。请在设置中选择当前方案并完成配置。"},
            status_code=422,
        )
    return None


@app.post("/api/job")
def submit_job(body: JobBody) -> JSONResponse:
    src = body.resolved()
    if not src:
        return JSONResponse({"error": "请输入视频链接或本地文件路径"}, status_code=422)
    guard = _stt_guard()
    if guard is not None:
        return guard

    is_url = src.startswith("http://") or src.startswith("https://")
    if is_url:
        # 用规范化身份键：同一视频的不同 URL 形态归并，复用缓存、免重复下载
        key = jobs.canonical_url_key(src)
        job, _ = jobs.registry.get_or_create(key, src, source_type="url")
    else:
        # 本地文件路径：以文件内容指纹为身份键，复用既有结果（与上传互通）
        p = Path(src).expanduser()
        if not p.exists() or not p.is_file():
            return JSONResponse({"error": f"本地文件不存在：{src}"}, status_code=422)
        if p.suffix.lower() not in _MEDIA_EXTS:
            return JSONResponse(
                {"error": f"不支持的文件类型「{p.suffix or '无扩展名'}」，请选择视频/音频文件"
                          f"（如 .mp4/.mkv/.mov/.webm/.mp3/.m4a 等）"},
                status_code=422,
            )
        key = jobs.file_fingerprint(p)
        job, _ = jobs.registry.get_or_create(key, str(p.resolve()), source_type="local")

    pipeline.start_job(job)
    return JSONResponse(job.snapshot())


@app.post("/api/job/upload")
async def upload_job(file: UploadFile = File(...)) -> JSONResponse:
    """上传本地视频/音频文件，保存后按本地文件流程处理。"""
    guard = _stt_guard()
    if guard is not None:
        return guard

    orig_name = Path(file.filename or "upload.mp4").name
    suffix = Path(orig_name).suffix.lower()
    if suffix not in _MEDIA_EXTS:
        return JSONResponse(
            {"error": f"不支持的文件类型「{suffix}」"}, status_code=422
        )

    content = await file.read()
    if not content:
        return JSONResponse({"error": "文件为空"}, status_code=422)

    uploads = media_dir() / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(content).hexdigest()  # 整文件指纹，与本地选择互通
    dest = uploads / f"{key[:16]}_{orig_name}"
    if not dest.exists():
        dest.write_bytes(content)

    job, _ = jobs.registry.get_or_create(key, str(dest.resolve()), source_type="upload")
    job.title = Path(orig_name).stem
    pipeline.start_job(job)
    return JSONResponse(job.snapshot())


@app.get("/api/job/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    job = jobs.registry.get(job_id)
    if job is None:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    return JSONResponse(job.snapshot())


@app.get("/api/job/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    job = jobs.registry.get(job_id)

    async def event_stream():
        if job is None:
            yield _sse({"type": "error", "message": "任务不存在"})
            return
        cursor = 0
        while True:
            new, cursor, finished = job.read_since(cursor)
            for ev in new:
                yield _sse(ev)
            if finished and not new:
                break
            await asyncio.sleep(0.25)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/video/{job_id}")
def serve_video(job_id: str, request: Request) -> Response:
    job = jobs.registry.get(job_id)
    if job is None or not job.video_path:
        return JSONResponse({"error": "视频尚未就绪"}, status_code=404)
    path = Path(job.video_path)
    if not path.exists():
        return JSONResponse({"error": "视频文件丢失"}, status_code=404)
    # FileResponse 已支持 Range 请求（Accept-Ranges/206），满足 <video> 拖动 seek
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/api/models-config")
def get_models_config() -> dict:
    return model_config.get_public()


@app.put("/api/models-config", response_model=None)
def put_models_config(body: ModelsConfigBody) -> JSONResponse | dict:
    payload: dict[str, Any] = {}
    if body.text is not None:
        payload["text"] = body.text
    if body.stt is not None:
        payload["stt"] = body.stt
    if body.download_proxy is not None:
        payload["download_proxy"] = body.download_proxy
    try:
        return model_config.save_config(payload)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)


@app.post("/api/models-config/reset")
def post_models_config_reset() -> dict:
    model_config.reset_config()
    return model_config.get_public()


@app.post("/api/models-config/test/text", response_model=None)
def post_test_text(body: TestTextBody) -> JSONResponse | dict:
    try:
        return model_config.test_text(body.task)
    except model_config.ConfigError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/api/models-config/test/stt", response_model=None)
def post_test_stt(body: TestSttBody) -> JSONResponse | dict:
    backend = body.backend.strip() or None
    try:
        return model_config.test_stt(backend, stt_draft=body.stt)
    except model_config.ConfigError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/api/models-config/test/proxy", response_model=None)
def post_test_proxy(body: TestProxyBody) -> JSONResponse | dict:
    try:
        return model_config.test_download_proxy(body.download_proxy)
    except model_config.ConfigError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/history")
def get_history() -> dict:
    return {"items": history.list_history()}


@app.post("/api/history")
def post_history(body: HistoryBody) -> dict:
    items = history.push_history(body.source, body.name)
    return {"items": items}


@app.post("/api/history/remove")
def post_history_remove(body: HistoryBody) -> dict:
    items = history.remove_history(body.source)
    return {"items": items}


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ── 前端静态资源 ──────────────────────────────────────────────────────────────
@app.get("/")
def index() -> FileResponse:
    return FileResponse(_FRONTEND / "index.html")


_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


@app.get("/style.css")
def style_css() -> FileResponse:
    return FileResponse(_FRONTEND / "style.css", media_type="text/css", headers=_NO_CACHE)


@app.get("/app.js")
def app_js() -> FileResponse:
    return FileResponse(_FRONTEND / "app.js", media_type="application/javascript", headers=_NO_CACHE)


@app.get("/learning-panel.js")
def learning_panel_js() -> FileResponse:
    return FileResponse(_FRONTEND / "learning-panel.js", media_type="application/javascript", headers=_NO_CACHE)


@app.get("/i18n.js")
def i18n_js() -> FileResponse:
    return FileResponse(_FRONTEND / "i18n.js", media_type="application/javascript", headers=_NO_CACHE)
