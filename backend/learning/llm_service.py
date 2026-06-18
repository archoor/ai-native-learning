"""学习工具 LLM 封装：按任务读取 model_config。"""

from __future__ import annotations

import json
import re
from typing import Any

from ..model_config import ConfigError, TextProfile, require_text, text_task_ready

MAX_INPUT_CHARS = 80_000


def llm_available() -> bool:
    return text_task_ready("outline")


def task_available(task: str) -> bool:
    return text_task_ready(task)


def _client(profile: TextProfile):
    from openai import OpenAI

    http_client = None
    try:
        import httpx
        http_client = httpx.Client(trust_env=False, timeout=120.0)
    except Exception:
        pass
    return OpenAI(base_url=profile.base_url, api_key=profile.api_key, http_client=http_client)


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def chat_json(
    system: str,
    user: str,
    *,
    task: str = "outline",
    temperature: float | None = None,
) -> Any:
    profile = require_text(task)
    client = _client(profile)
    if len(user) > MAX_INPUT_CHARS:
        user = user[:MAX_INPUT_CHARS] + "\n\n[…下文已截断…]"
    temp = profile.temperature if temperature is None else temperature
    resp = client.chat.completions.create(
        model=profile.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temp,
        max_tokens=profile.max_tokens,
        response_format={"type": "json_object"},
    )
    content = (resp.choices[0].message.content or "").strip()
    if not content:
        raise RuntimeError("LLM 返回空内容")
    return _extract_json(content)


def chat_text(
    system: str,
    user: str,
    *,
    task: str = "report",
    temperature: float | None = None,
) -> str:
    profile = require_text(task)
    client = _client(profile)
    temp = profile.temperature if temperature is None else temperature
    resp = client.chat.completions.create(
        model=profile.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temp,
        max_tokens=profile.max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()


def chat_messages(
    system: str,
    messages: list[dict[str, str]],
    *,
    task: str = "grade",
    temperature: float | None = None,
    json_mode: bool = False,
) -> str | Any:
    profile = require_text(task)
    client = _client(profile)
    full = [{"role": "system", "content": system}, *messages]
    temp = profile.temperature if temperature is None else temperature
    kwargs: dict[str, Any] = {
        "model": profile.model,
        "messages": full,
        "temperature": temp,
        "max_tokens": profile.max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    content = (resp.choices[0].message.content or "").strip()
    if not content:
        raise RuntimeError("LLM 返回空内容")
    return _extract_json(content) if json_mode else content
