"""关联高亮：分批并行 + 流式推送。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterator

from . import llm_service, prompts

BATCH_SIZE = 25
MAX_PARALLEL = 3


def _highlight_batch(goal: str, batch: list[dict]) -> list[dict]:
    transcript = prompts.format_segments_for_llm(batch)
    user = f"学习目标：{goal}\n\n<transcript>\n{transcript}\n</transcript>"
    result = llm_service.chat_json(prompts.HIGHLIGHT_SYSTEM, user, task="highlight", temperature=0.2)
    return result.get("segments") or []


def highlight_segments_stream(goal: str, segments: list[dict]) -> Iterator[dict[str, Any]]:
    """
    生成 SSE 事件 dict：
      progress | batch | done | error
    """
    if not segments:
        yield {"type": "done", "highlights": {"segments": []}, "goal": goal}
        return

    batches: list[list[dict]] = []
    for i in range(0, len(segments), BATCH_SIZE):
        batches.append(segments[i : i + BATCH_SIZE])

    total = len(segments)
    done_count = 0
    merged: list[dict] = []

    yield {
        "type": "progress",
        "done": 0,
        "total": total,
        "batches": len(batches),
        "message": f"开始标注，共 {total} 段，分 {len(batches)} 批并行…",
    }

    workers = min(MAX_PARALLEL, len(batches))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_highlight_batch, goal, b): b for b in batches}
        for fut in as_completed(futures):
            try:
                part = fut.result()
            except Exception as e:
                yield {"type": "error", "message": str(e)}
                return
            batch = futures[fut]
            merged.extend(part)
            done_count += len(batch)
            yield {
                "type": "batch",
                "segments": part,
                "done": min(done_count, total),
                "total": total,
            }

    merged.sort(key=lambda x: x.get("index", 0))
    yield {
        "type": "done",
        "goal": goal,
        "highlights": {"segments": merged},
        "done": total,
        "total": total,
    }


def highlight_segments_sync(goal: str, segments: list[dict]) -> dict[str, Any]:
    """非流式：内部分批并行，合并结果。"""
    result_segments: list[dict] = []
    for ev in highlight_segments_stream(goal, segments):
        if ev["type"] == "batch":
            result_segments.extend(ev.get("segments") or [])
        elif ev["type"] == "error":
            raise RuntimeError(ev.get("message", "标注失败"))
        elif ev["type"] == "done":
            return {"goal": goal, "highlights": ev["highlights"]}
    return {"goal": goal, "highlights": {"segments": result_segments}}
