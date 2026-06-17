"""学习工具 API 路由。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from .. import jobs  # noqa: E402
from ..live_transcribe import cloud_stt_available  # noqa: E402
from . import kb_writer  # noqa: E402
from . import highlight_service, llm_service, prompts, voice_stt  # noqa: E402
from .session import store  # noqa: E402

router = APIRouter(prefix="/api/learn", tags=["learn"])


class GoalBody(BaseModel):
    goal: str = Field(..., min_length=1, max_length=500)


class QuestionsBody(BaseModel):
    goal: str = Field(..., min_length=1, max_length=500)
    count: int = Field(5, ge=1, le=20)


class GradeBody(BaseModel):
    answers: list[dict[str, Any]] = Field(..., min_length=1)


class GradeChatBody(BaseModel):
    question_id: int
    message: str = Field(..., min_length=1, max_length=2000)


class GradeConfirmBody(BaseModel):
    confirmations: list[dict[str, Any]] = Field(..., min_length=1)


class FeynmanBody(BaseModel):
    draft: str = Field(..., min_length=10, max_length=20000)


class ChatBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


def _public_session(sess) -> dict:
    data = sess.to_dict()
    if data.get("questions"):
        data["questions"] = [{"id": q["id"], "text": q["text"]} for q in data["questions"]]
    return data


def _job_segments(job_id: str) -> tuple[Any, list[dict] | JSONResponse]:
    job = jobs.registry.get(job_id)
    if job is None:
        return None, JSONResponse({"error": "任务不存在"}, status_code=404)
    segs = [s for s in job.segments if s and s.get("source")]
    if not segs:
        return job, JSONResponse({"error": "转录稿尚未就绪，请等待字幕生成"}, status_code=422)
    return job, segs


@router.get("/health")
def learn_health() -> dict:
    vs = voice_stt.voice_status()
    return {
        "ok": True,
        "llm": llm_service.llm_available(),
        "stt": cloud_stt_available(),
        "voice": vs,
    }


def _sse_line(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@router.get("/{job_id}/session")
def get_session(job_id: str) -> dict:
    sess = store.get(job_id)
    job = jobs.registry.get(job_id)
    if job and job.title:
        sess.title = job.title
    return _public_session(sess)


@router.post("/{job_id}/outline")
def post_outline(job_id: str) -> JSONResponse:
    if not llm_service.llm_available():
        return JSONResponse({"error": "未配置 LLM（SUMMARY_*）"}, status_code=422)
    job, segs_or_resp = _job_segments(job_id)
    if isinstance(segs_or_resp, JSONResponse):
        return segs_or_resp
    segs = segs_or_resp

    transcript = prompts.format_segments_for_llm(segs)
    title = job.title or "未命名视频"
    user = f"视频标题：{title}\n\n请用简体中文输出 skeleton 与 map.summary。\n\n<transcript>\n{transcript}\n</transcript>"
    try:
        result = llm_service.chat_json(prompts.OUTLINE_SYSTEM, user)
    except Exception as e:
        return JSONResponse({"error": f"生成骨架失败：{e}"}, status_code=502)

    sess = store.get(job_id)
    sess.title = title
    sess.outline = result
    store.save(sess)
    return JSONResponse({"outline": result})


@router.post("/{job_id}/highlight")
def post_highlight(job_id: str, body: GoalBody) -> JSONResponse:
    if not llm_service.llm_available():
        return JSONResponse({"error": "未配置 LLM（SUMMARY_*）"}, status_code=422)
    job, segs_or_resp = _job_segments(job_id)
    if isinstance(segs_or_resp, JSONResponse):
        return segs_or_resp
    segs = segs_or_resp
    goal = body.goal.strip()
    try:
        result = highlight_service.highlight_segments_sync(goal, segs)
    except Exception as e:
        return JSONResponse({"error": f"关联标注失败：{e}"}, status_code=502)

    sess = store.get(job_id)
    sess.goal = goal
    sess.highlights = result["highlights"]
    store.save(sess)
    return JSONResponse({"goal": sess.goal, "highlights": result["highlights"]})


@router.post("/{job_id}/highlight/stream")
def post_highlight_stream(job_id: str, body: GoalBody) -> StreamingResponse:
    """分批并行标注，SSE 流式返回进度与部分结果。"""
    if not llm_service.llm_available():
        return JSONResponse({"error": "未配置 LLM（SUMMARY_*）"}, status_code=422)  # type: ignore[return-value]

    job, segs_or_resp = _job_segments(job_id)
    if isinstance(segs_or_resp, JSONResponse):
        return segs_or_resp  # type: ignore[return-value]

    goal = body.goal.strip()
    segs = segs_or_resp

    def event_stream():
        sess = store.get(job_id)
        merged: list[dict] = []
        for ev in highlight_service.highlight_segments_stream(goal, segs):
            if ev["type"] == "batch":
                merged.extend(ev.get("segments") or [])
                ev = {**ev, "highlights": {"segments": sorted(merged, key=lambda x: x.get("index", 0))}}
            yield _sse_line(ev)
            if ev["type"] == "done":
                sess.goal = goal
                sess.highlights = ev["highlights"]
                store.save(sess)
            elif ev["type"] == "error":
                break

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{job_id}/questions")
def post_questions(job_id: str, body: QuestionsBody) -> JSONResponse:
    if not llm_service.llm_available():
        return JSONResponse({"error": "未配置 LLM（SUMMARY_*）"}, status_code=422)
    job, segs_or_resp = _job_segments(job_id)
    if isinstance(segs_or_resp, JSONResponse):
        return segs_or_resp
    segs = segs_or_resp

    # 优先用高相关段，否则全文
    sess = store.get(job_id)
    hi = sess.highlights or {}
    rel_map = {item["index"]: item.get("relevance", 0) for item in hi.get("segments", [])}
    focused = [s for s in segs if rel_map.get(s.get("index"), 0) >= 1] or segs
    if sess.outline and sess.outline.get("skeleton"):
        skeleton = "\n".join(f"- {x}" for x in sess.outline["skeleton"])
    else:
        skeleton = ""

    transcript = prompts.format_segments_for_llm(focused)
    user = (
        f"学习目标：{body.goal.strip()}\n"
        f"题目数量：{body.count}\n\n"
        + (f"骨架：\n{skeleton}\n\n" if skeleton else "")
        + f"<transcript>\n{transcript}\n</transcript>"
    )
    try:
        result = llm_service.chat_json(prompts.QUESTIONS_SYSTEM, user)
    except Exception as e:
        return JSONResponse({"error": f"出题失败：{e}"}, status_code=502)

    questions = result.get("questions") or []
    sess.goal = body.goal.strip()
    sess.questions = questions
    store.save(sess)

    public = [{"id": q.get("id", i + 1), "text": q.get("text", "")} for i, q in enumerate(questions)]
    return JSONResponse({"goal": sess.goal, "questions": public})


@router.post("/transcribe-answer")
async def transcribe_answer(file: UploadFile = File(...)) -> JSONResponse:
    """语音答案 → 文字（优先本地 Whisper，降级云端 STT）。"""
    vs = voice_stt.voice_status()
    if not vs["available"]:
        return JSONResponse({"error": "转写模型不可用"}, status_code=422)

    suffix = Path(file.filename or "answer.webm").suffix or ".webm"
    content = await file.read()
    if not content:
        return JSONResponse({"error": "音频为空"}, status_code=422)

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        text, backend = voice_stt.transcribe_file(tmp_path, language="zh")
    except Exception as e:
        return JSONResponse({"error": str(e) or "转写模型不可用"}, status_code=502)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return JSONResponse({"text": text, "backend": backend})


@router.post("/{job_id}/grade")
def post_grade(job_id: str, body: GradeBody) -> JSONResponse:
    if not llm_service.llm_available():
        return JSONResponse({"error": "未配置 LLM"}, status_code=422)
    sess = store.get(job_id)
    if not sess.questions:
        return JSONResponse({"error": "请先生成自测题"}, status_code=422)

    qmap = {q.get("id", i + 1): q for i, q in enumerate(sess.questions)}
    lines = []
    for ans in body.answers:
        qid = ans.get("id")
        q = qmap.get(qid)
        if not q:
            continue
        lines.append(
            f"Q{qid}: {q.get('text', '')}\n"
            f"参考答案: {q.get('reference_answer', '')}\n"
            f"学习者答案: {ans.get('answer', '').strip() or '（未作答）'}\n"
        )
    user = f"学习目标：{sess.goal}\n\n" + "\n".join(lines)
    try:
        result = llm_service.chat_json(prompts.GRADE_ANSWERS_SYSTEM, user)
    except Exception as e:
        return JSONResponse({"error": f"批改失败：{e}"}, status_code=502)

    sess.quiz_answers = body.answers
    sess.quiz_grades = result.get("grades") or []
    store.save(sess)
    return JSONResponse(result)


@router.post("/{job_id}/grade/chat")
def post_grade_chat(job_id: str, body: GradeChatBody) -> JSONResponse:
    if not llm_service.llm_available():
        return JSONResponse({"error": "未配置 LLM"}, status_code=422)
    sess = store.get(job_id)
    qid = str(body.question_id)
    q = next((x for x in sess.questions if x.get("id") == body.question_id), None)
    grade = next((g for g in sess.quiz_grades if g.get("id") == body.question_id), None)
    if not q:
        return JSONResponse({"error": "题目不存在"}, status_code=404)

    history = sess.quiz_chats.get(qid, [])
    context = (
        f"题目：{q.get('text')}\n参考答案：{q.get('reference_answer')}\n"
        f"初次批改：{grade}\n"
    )
    messages = [{"role": "user", "content": context}]
    for h in history[-8:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": body.message})

    try:
        reply = llm_service.chat_messages(prompts.GRADE_CHAT_SYSTEM, messages)
    except Exception as e:
        return JSONResponse({"error": f"对话失败：{e}"}, status_code=502)

    history.append({"role": "user", "content": body.message})
    history.append({"role": "assistant", "content": reply})
    sess.quiz_chats[qid] = history
    store.save(sess)
    return JSONResponse({"reply": reply, "history": history})


@router.post("/{job_id}/grade/confirm")
def post_grade_confirm(job_id: str, body: GradeConfirmBody) -> JSONResponse:
    sess = store.get(job_id)
    existing = {c.get("id"): c for c in sess.quiz_confirmed}
    for c in body.confirmations:
        existing[c.get("id")] = {"id": c.get("id"), "answer": c.get("answer", "").strip()}
    sess.quiz_confirmed = list(existing.values())
    store.save(sess)
    return JSONResponse({"confirmed": sess.quiz_confirmed})


@router.post("/{job_id}/feynman/grade")
def post_feynman_grade(job_id: str, body: FeynmanBody) -> JSONResponse:
    if not llm_service.llm_available():
        return JSONResponse({"error": "未配置 LLM"}, status_code=422)
    sess = store.get(job_id)
    user = (
        f"学习目标：{sess.goal}\n\n"
        f"骨架：\n"
        + "\n".join(f"- {s}" for s in (sess.outline or {}).get("skeleton") or [])
        + f"\n\n学习者费曼复述：\n{body.draft.strip()}"
    )
    try:
        result = llm_service.chat_json(prompts.FEYNMAN_GRADE_SYSTEM, user)
    except Exception as e:
        return JSONResponse({"error": f"费曼批改失败：{e}"}, status_code=502)

    sess.feynman_draft = body.draft.strip()
    sess.feynman_grade = result
    store.save(sess)
    return JSONResponse(result)


@router.post("/{job_id}/feynman/chat")
def post_feynman_chat(job_id: str, body: ChatBody) -> JSONResponse:
    if not llm_service.llm_available():
        return JSONResponse({"error": "未配置 LLM"}, status_code=422)
    sess = store.get(job_id)
    history = list(sess.feynman_chat)
    context = f"学习目标：{sess.goal}\n费曼草稿：\n{sess.feynman_draft}\n初次批改：{sess.feynman_grade}\n"
    messages = [{"role": "user", "content": context}]
    for h in history[-10:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": body.message})
    try:
        reply = llm_service.chat_messages(prompts.FEYNMAN_CHAT_SYSTEM, messages)
    except Exception as e:
        return JSONResponse({"error": f"对话失败：{e}"}, status_code=502)

    history.append({"role": "user", "content": body.message})
    history.append({"role": "assistant", "content": reply})
    sess.feynman_chat = history
    store.save(sess)
    return JSONResponse({"reply": reply, "history": history})


@router.post("/{job_id}/feynman/confirm")
def post_feynman_confirm(job_id: str, body: FeynmanBody) -> JSONResponse:
    sess = store.get(job_id)
    sess.feynman_confirmed = body.draft.strip()
    store.save(sess)
    return JSONResponse({"ok": True})


@router.post("/{job_id}/report")
def post_report(job_id: str) -> JSONResponse:
    if not llm_service.llm_available():
        return JSONResponse({"error": "未配置 LLM"}, status_code=422)
    sess = store.get(job_id)
    job = jobs.registry.get(job_id)
    title = sess.title or (job.title if job else "") or "未命名视频"
    payload = {
        "title": title,
        "goal": sess.goal,
        "skeleton": (sess.outline or {}).get("skeleton"),
        "quiz_grades": sess.quiz_grades,
        "quiz_confirmed": sess.quiz_confirmed,
        "feynman_grade": sess.feynman_grade,
        "feynman_confirmed": sess.feynman_confirmed[:500] if sess.feynman_confirmed else "",
    }
    user = f"学习记录 JSON：\n{payload}"
    try:
        report = llm_service.chat_text(
            prompts.REPORT_SYSTEM.replace("{标题}", title),
            user,
        )
    except Exception as e:
        return JSONResponse({"error": f"报告生成失败：{e}"}, status_code=502)

    sess.report = report
    store.save(sess)
    return JSONResponse({"report": report})


@router.post("/{job_id}/persist")
def post_persist(job_id: str) -> JSONResponse:
    job, segs_or_resp = _job_segments(job_id)
    if isinstance(segs_or_resp, JSONResponse):
        return segs_or_resp
    sess = store.get(job_id)
    if job and job.title:
        sess.title = job.title
    if not sess.goal:
        return JSONResponse({"error": "请先完成学习目标与自测"}, status_code=422)
    try:
        paths = kb_writer.persist_session(sess, segs_or_resp)
    except Exception as e:
        return JSONResponse({"error": f"入库失败：{e}"}, status_code=500)

    sess.persisted = paths
    store.save(sess)
    return JSONResponse({"paths": paths})
