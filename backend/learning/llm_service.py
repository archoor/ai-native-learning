"""学习工具 LLM 封装：复用 .env 中 SUMMARY_* 配置（deepseek-v4-pro）。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.lib.transcript_common import get_llm_config  # noqa: E402

MAX_INPUT_CHARS = 80_000


def llm_available() -> bool:
    return get_llm_config() is not None


def _client():
    cfg = get_llm_config()
    if cfg is None:
        raise RuntimeError("未配置 LLM（SUMMARY_BASE_URL / SUMMARY_API_KEY / SUMMARY_MODEL）")
    model, base_url, api_key = cfg
    from openai import OpenAI

    return OpenAI(base_url=base_url, api_key=api_key), model


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def chat_json(system: str, user: str, *, temperature: float = 0.3) -> Any:
    """调用 LLM 并解析 JSON 响应。"""
    client, model = _client()
    if len(user) > MAX_INPUT_CHARS:
        user = user[:MAX_INPUT_CHARS] + "\n\n[…下文已截断…]"
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        response_format={"type": "json_object"},
    )
    content = (resp.choices[0].message.content or "").strip()
    if not content:
        raise RuntimeError("LLM 返回空内容")
    return _extract_json(content)


def chat_text(system: str, user: str, *, temperature: float = 0.3) -> str:
    client, model = _client()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
    )
    return (resp.choices[0].message.content or "").strip()


def chat_messages(
    system: str,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.4,
    json_mode: bool = False,
) -> str | Any:
    """多轮对话；json_mode=True 时解析 JSON。"""
    client, model = _client()
    full = [{"role": "system", "content": system}, *messages]
    kwargs: dict[str, Any] = {"model": model, "messages": full, "temperature": temperature}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    content = (resp.choices[0].message.content or "").strip()
    if not content:
        raise RuntimeError("LLM 返回空内容")
    return _extract_json(content) if json_mode else content
