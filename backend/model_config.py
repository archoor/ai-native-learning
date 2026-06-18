"""统一模型配置：文本 LLM + 转录 STT + 下载代理，仅存面板 JSON，不读 .env。"""

from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .paths import user_data_dir

_CONFIG_VERSION = 1
_CONFIG_FILE = user_data_dir() / "models_config.json"
_lock = threading.Lock()

TEXT_TASKS = (
    "translate",
    "outline",
    "highlight",
    "quiz",
    "grade",
    "feynman",
    "report",
)
STT_BACKENDS = ("whisper", "cloud", "funasr")
SttBackend = Literal["whisper", "cloud", "funasr"]

TEMP_MIN, TEMP_MAX = 0.0, 2.0
MAX_TOKENS_MIN, MAX_TOKENS_MAX = 256, 128_000
DEFAULT_TEMPERATURE = 0.3
DEFAULT_MAX_TOKENS = 4096
DEFAULT_STT_TIMEOUT = 120

_URL_RE = re.compile(r"^https?://", re.I)
_PROXY_RE = re.compile(r"^(socks5h?|https?)://", re.I)

TASK_LABELS: dict[str, str] = {
    "translate": "字幕翻译",
    "outline": "骨架 / 信息地图",
    "highlight": "主题高亮",
    "quiz": "自测出题",
    "grade": "答案批改",
    "feynman": "费曼学习",
    "report": "学习报告",
}

STT_LABELS: dict[str, str] = {
    "whisper": "本地 Whisper",
    "cloud": "云端 STT",
    "funasr": "Fun-ASR",
}


class ConfigError(RuntimeError):
    """配置缺失或不完整。"""


@dataclass(frozen=True)
class TextProfile:
    task: str
    model: str
    base_url: str
    api_key: str
    temperature: float
    max_tokens: int


@dataclass(frozen=True)
class SttProfile:
    backend: SttBackend
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_sec: int = DEFAULT_STT_TIMEOUT
    language_hint: str = ""
    oss_endpoint: str = ""
    oss_bucket: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""


def _empty_config() -> dict[str, Any]:
    return {"version": _CONFIG_VERSION, "text": {}, "stt": {}, "download_proxy": {}}


def _load_raw() -> dict[str, Any]:
    if not _CONFIG_FILE.exists():
        return _empty_config()
    try:
        data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty_config()
        data.setdefault("version", _CONFIG_VERSION)
        data.setdefault("text", {})
        data.setdefault("stt", {})
        data.setdefault("download_proxy", {})
        return data
    except Exception:
        return _empty_config()


def _save_raw(data: dict[str, Any]) -> None:
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "version": _CONFIG_VERSION,
        "text": data.get("text") or {},
        "stt": data.get("stt") or {},
        "download_proxy": data.get("download_proxy") or {},
    }
    _CONFIG_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def _norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _api_key_hint(key: str) -> str:
    if len(key) <= 4:
        return "****"
    return f"···{key[-4:]}"


def _merge_text(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(existing)
    for k in ("base_url", "temperature", "max_tokens"):
        if k in incoming and incoming[k] is not None:
            out[k] = incoming[k]
    if "api_key" in incoming:
        key = str(incoming.get("api_key") or "").strip()
        if key:
            out["api_key"] = key
    if "tasks" in incoming and isinstance(incoming["tasks"], dict):
        tasks = dict(out.get("tasks") or {})
        for task, spec in incoming["tasks"].items():
            if task not in TEXT_TASKS or not isinstance(spec, dict):
                continue
            cur = dict(tasks.get(task) or {})
            model = str(spec.get("model") or "").strip()
            if model:
                cur["model"] = model
            tasks[task] = cur
        out["tasks"] = tasks
    return out


def _merge_stt(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(existing)
    if "active" in incoming and incoming["active"] in STT_BACKENDS:
        out["active"] = incoming["active"]
    for backend in STT_BACKENDS:
        key = backend
        if key not in incoming or not isinstance(incoming[key], dict):
            continue
        cur = dict(out.get(key) or {})
        inc = incoming[key]
        for field in (
            "base_url", "api_key", "model", "language_hint",
            "oss_endpoint", "oss_bucket", "oss_access_key_id", "oss_access_key_secret",
        ):
            if field in inc:
                val = str(inc.get(field) or "").strip()
                if val or field.endswith("_secret") or field == "api_key":
                    if val:
                        cur[field] = val
        if "timeout_sec" in inc and inc["timeout_sec"] is not None:
            cur["timeout_sec"] = int(inc["timeout_sec"])
        out[key] = cur
    return out


def _merge_download_proxy(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(existing)
    if "enabled" in incoming:
        out["enabled"] = bool(incoming["enabled"])
    if "url" in incoming:
        out["url"] = str(incoming.get("url") or "").strip()
    return out


def validate_download_proxy_block(block: dict[str, Any]) -> None:
    if not block.get("enabled"):
        return
    url = str(block.get("url") or "").strip()
    if not url:
        raise ValueError("已启用备用下载代理，请填写代理地址")
    if not _PROXY_RE.match(url):
        raise ValueError("代理地址须以 socks5://、socks5h://、http:// 或 https:// 开头")


def get_download_fallback_proxy() -> str | None:
    """直连失败时使用的备用 yt-dlp 代理；未启用或未配置则返回 None。"""
    block = _load_raw().get("download_proxy") or {}
    if not block.get("enabled"):
        return None
    url = str(block.get("url") or "").strip()
    return url or None


def validate_text_block(text: dict[str, Any]) -> None:
    base_url = _norm_url(str(text.get("base_url") or ""))
    api_key = str(text.get("api_key") or "").strip()
    if not base_url or not _URL_RE.match(base_url):
        raise ValueError("文本模型 Base URL 无效")
    if not api_key:
        raise ValueError("文本模型 API Key 未配置")
    try:
        temp = float(text.get("temperature", DEFAULT_TEMPERATURE))
    except (TypeError, ValueError) as e:
        raise ValueError("temperature 必须是数字") from e
    if not (TEMP_MIN <= temp <= TEMP_MAX):
        raise ValueError(f"temperature 须在 {TEMP_MIN} ~ {TEMP_MAX} 之间")
    try:
        max_tokens = int(text.get("max_tokens", DEFAULT_MAX_TOKENS))
    except (TypeError, ValueError) as e:
        raise ValueError("max_tokens 必须是整数") from e
    if not (MAX_TOKENS_MIN <= max_tokens <= MAX_TOKENS_MAX):
        raise ValueError(f"max_tokens 须在 {MAX_TOKENS_MIN} ~ {MAX_TOKENS_MAX} 之间")
    tasks = text.get("tasks") or {}
    for task in TEXT_TASKS:
        model = str((tasks.get(task) or {}).get("model") or "").strip()
        if not model:
            raise ValueError(f"任务「{TASK_LABELS[task]}」未配置模型")


def _stt_missing_fields(backend: str, block: dict[str, Any] | None) -> list[str]:
    block = block or {}
    if backend == "whisper":
        if not _norm_url(str(block.get("base_url") or "")):
            return ["服务 URL"]
        return []
    if backend == "cloud":
        missing: list[str] = []
        if not _norm_url(str(block.get("base_url") or "")):
            missing.append("Base URL")
        if not str(block.get("api_key") or "").strip():
            missing.append("API Key")
        if not str(block.get("model") or "").strip():
            missing.append("模型")
        return missing
    if backend == "funasr":
        missing = []
        if not str(block.get("api_key") or "").strip():
            missing.append("DashScope API Key")
        if not str(block.get("oss_endpoint") or "").strip():
            missing.append("OSS Endpoint")
        if not str(block.get("oss_bucket") or "").strip():
            missing.append("Bucket")
        if not str(block.get("oss_access_key_id") or "").strip():
            missing.append("Access Key ID")
        if not str(block.get("oss_access_key_secret") or "").strip():
            missing.append("Access Key Secret")
        return missing
    return ["未知字段"]


def _whisper_ready(block: dict[str, Any]) -> bool:
    return not _stt_missing_fields("whisper", block)


def _cloud_ready(block: dict[str, Any]) -> bool:
    return not _stt_missing_fields("cloud", block)


def _funasr_ready(block: dict[str, Any]) -> bool:
    return not _stt_missing_fields("funasr", block)


def stt_backend_ready(backend: str, stt: dict[str, Any] | None = None) -> bool:
    stt = stt if stt is not None else (_load_raw().get("stt") or {})
    block = stt.get(backend) or {}
    if backend == "whisper":
        return _whisper_ready(block)
    if backend == "cloud":
        return _cloud_ready(block)
    if backend == "funasr":
        return _funasr_ready(block)
    return False


def text_task_ready(task: str, cfg: dict[str, Any] | None = None) -> bool:
    if task not in TEXT_TASKS:
        return False
    cfg = cfg if cfg is not None else _load_raw()
    text = cfg.get("text") or {}
    if not str(text.get("base_url") or "").strip() or not str(text.get("api_key") or "").strip():
        return False
    model = str(((text.get("tasks") or {}).get(task) or {}).get("model") or "").strip()
    return bool(model)


def text_ready(cfg: dict[str, Any] | None = None) -> bool:
    cfg = cfg if cfg is not None else _load_raw()
    text = cfg.get("text") or {}
    try:
        validate_text_block(text)
        return True
    except ValueError:
        return False


def stt_active_ready(cfg: dict[str, Any] | None = None) -> bool:
    cfg = cfg if cfg is not None else _load_raw()
    stt = cfg.get("stt") or {}
    active = stt.get("active")
    if active not in STT_BACKENDS:
        return False
    return stt_backend_ready(active, stt)


def save_config(body: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        raw = _load_raw()
        if "text" in body and isinstance(body["text"], dict):
            raw["text"] = _merge_text(raw.get("text") or {}, body["text"])
        if "stt" in body and isinstance(body["stt"], dict):
            raw["stt"] = _merge_stt(raw.get("stt") or {}, body["stt"])
        if "download_proxy" in body and isinstance(body["download_proxy"], dict):
            raw["download_proxy"] = _merge_download_proxy(
                raw.get("download_proxy") or {}, body["download_proxy"]
            )
            validate_download_proxy_block(raw["download_proxy"])
            # 先落盘代理配置，避免其它区块校验失败导致代理无法保存
            _save_raw(raw)
        stt = raw.get("stt") or {}
        active = stt.get("active")
        if active and active in STT_BACKENDS and not stt_backend_ready(active, stt):
            missing = _stt_missing_fields(active, stt.get(active) or {})
            raise ValueError(
                f"当前转录方案「{STT_LABELS[active]}」配置不完整：缺少 {', '.join(missing)}"
            )
        if (raw.get("text") or {}) and text_ready(raw):
            validate_text_block(raw["text"])
        _save_raw(raw)
        return get_public()


def reset_config() -> None:
    with _lock:
        if _CONFIG_FILE.exists():
            _CONFIG_FILE.unlink()


def get_public() -> dict[str, Any]:
    raw = _load_raw()
    text = raw.get("text") or {}
    stt = raw.get("stt") or {}
    dp = raw.get("download_proxy") or {}
    api_key = str(text.get("api_key") or "")
    public_text = {
        "base_url": _norm_url(str(text.get("base_url") or "")),
        "api_key_configured": bool(api_key),
        "api_key_hint": _api_key_hint(api_key) if api_key else "",
        "temperature": float(text.get("temperature", DEFAULT_TEMPERATURE)),
        "max_tokens": int(text.get("max_tokens", DEFAULT_MAX_TOKENS)),
        "tasks": {
            t: {"model": str(((text.get("tasks") or {}).get(t) or {}).get("model") or "")}
            for t in TEXT_TASKS
        },
    }
    public_stt: dict[str, Any] = {
        "active": stt.get("active") or "",
        "backends": {b: stt_backend_ready(b, stt) for b in STT_BACKENDS},
    }
    for b in STT_BACKENDS:
        block = dict(stt.get(b) or {})
        if block.get("api_key"):
            block = {**block, "api_key_configured": True, "api_key_hint": _api_key_hint(str(block["api_key"]))}
            block.pop("api_key", None)
        if block.get("oss_access_key_secret"):
            block = {**block, "oss_secret_configured": True}
            block.pop("oss_access_key_secret", None)
        public_stt[b] = block
    public_proxy = {
        "enabled": bool(dp.get("enabled")),
        "url": str(dp.get("url") or ""),
    }
    return {
        "version": _CONFIG_VERSION,
        "text": public_text,
        "stt": public_stt,
        "download_proxy": public_proxy,
        "readiness": readiness(raw),
        "limits": {
            "temperature": [TEMP_MIN, TEMP_MAX],
            "max_tokens": [MAX_TOKENS_MIN, MAX_TOKENS_MAX],
        },
        "task_labels": TASK_LABELS,
        "stt_labels": STT_LABELS,
    }


def readiness(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg if cfg is not None else _load_raw()
    stt = cfg.get("stt") or {}
    active = stt.get("active") or ""
    return {
        "text_ready": text_ready(cfg),
        "translate_ready": text_task_ready("translate", cfg),
        "learn_ready": all(text_task_ready(t, cfg) for t in TEXT_TASKS if t != "translate"),
        "stt_ready": stt_active_ready(cfg),
        "stt_active": active,
        "stt_backends": {b: stt_backend_ready(b, stt) for b in STT_BACKENDS},
        "tasks": {t: text_task_ready(t, cfg) for t in TEXT_TASKS},
    }


def require_text(task: str) -> TextProfile:
    if task not in TEXT_TASKS:
        raise ConfigError(f"未知文本任务：{task}")
    raw = _load_raw()
    text = raw.get("text") or {}
    try:
        validate_text_block(text)
    except ValueError as e:
        raise ConfigError(str(e)) from e
    model = str(((text.get("tasks") or {}).get(task) or {}).get("model") or "").strip()
    if not model:
        raise ConfigError(f"任务「{TASK_LABELS[task]}」未配置模型")
    return TextProfile(
        task=task,
        model=model,
        base_url=_norm_url(str(text.get("base_url") or "")),
        api_key=str(text.get("api_key") or ""),
        temperature=float(text.get("temperature", DEFAULT_TEMPERATURE)),
        max_tokens=int(text.get("max_tokens", DEFAULT_MAX_TOKENS)),
    )


def _profile_from_block(backend: SttBackend, block: dict[str, Any]) -> SttProfile:
    if backend == "whisper":
        return SttProfile(
            backend="whisper",
            base_url=_norm_url(str(block.get("base_url") or "")),
            model=str(block.get("model") or "").strip(),
            timeout_sec=int(block.get("timeout_sec") or DEFAULT_STT_TIMEOUT),
        )
    if backend == "cloud":
        return SttProfile(
            backend="cloud",
            base_url=_norm_url(str(block.get("base_url") or "")),
            api_key=str(block.get("api_key") or ""),
            model=str(block.get("model") or "").strip(),
            timeout_sec=int(block.get("timeout_sec") or DEFAULT_STT_TIMEOUT),
            language_hint=str(block.get("language_hint") or "").strip(),
        )
    return SttProfile(
        backend="funasr",
        api_key=str(block.get("api_key") or ""),
        model=str(block.get("model") or "").strip() or "fun-asr",
        oss_endpoint=str(block.get("oss_endpoint") or "").strip(),
        oss_bucket=str(block.get("oss_bucket") or "").strip(),
        oss_access_key_id=str(block.get("oss_access_key_id") or "").strip(),
        oss_access_key_secret=str(block.get("oss_access_key_secret") or "").strip(),
        timeout_sec=int(block.get("timeout_sec") or DEFAULT_STT_TIMEOUT),
        language_hint=str(block.get("language_hint") or "").strip(),
    )


def require_stt() -> SttProfile:
    raw = _load_raw()
    stt = raw.get("stt") or {}
    active = stt.get("active")
    if active not in STT_BACKENDS:
        raise ConfigError("未选择当前转录方案，请在设置中指定")
    if not stt_backend_ready(active, stt):
        missing = _stt_missing_fields(active, stt.get(active) or {})
        raise ConfigError(
            f"当前转录方案「{STT_LABELS[active]}」配置不完整：缺少 {', '.join(missing)}"
        )
    return _profile_from_block(active, stt.get(active) or {})


@contextmanager
def funasr_env(profile: SttProfile):
    """临时注入 fun-asr 所需环境变量（供 scripts 复用）。"""
    import os

    mapping = {
        "DASHSCOPE_API_KEY": profile.api_key,
        "DASHSCOPE_BASE_URL": "https://dashscope.aliyuncs.com/api/v1",
        "OSS_ENDPOINT": profile.oss_endpoint,
        "OSS_BUCKET": profile.oss_bucket,
        "OSS_ACCESS_KEY_ID": profile.oss_access_key_id,
        "OSS_ACCESS_KEY_SECRET": profile.oss_access_key_secret,
    }
    old: dict[str, str | None] = {}
    for k, v in mapping.items():
        old[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _openai_client(base_url: str, api_key: str):
    from openai import OpenAI

    http_client = None
    try:
        import httpx
        http_client = httpx.Client(trust_env=False, timeout=60.0)
    except Exception:
        pass
    return OpenAI(base_url=base_url, api_key=api_key, http_client=http_client)


def test_text(task: str | None = None) -> dict[str, Any]:
    task = task or "translate"
    prof = require_text(task)
    client = _openai_client(prof.base_url, prof.api_key)
    kwargs: dict[str, Any] = {
        "model": prof.model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "temperature": prof.temperature,
    }
    if task != "translate":
        kwargs["response_format"] = {"type": "json_object"}
        kwargs["messages"] = [
            {"role": "system", "content": "Reply with JSON: {\"ok\":true}"},
            {"role": "user", "content": "ping"},
        ]
    resp = client.chat.completions.create(**kwargs)
    content = (resp.choices[0].message.content or "").strip()
    return {"ok": True, "task": task, "model": prof.model, "sample": content[:80]}


def test_stt(backend: str | None = None, stt_draft: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = _load_raw()
    stt = (
        _merge_stt(raw.get("stt") or {}, stt_draft)
        if isinstance(stt_draft, dict)
        else (raw.get("stt") or {})
    )
    use = backend or stt.get("active")
    if use not in STT_BACKENDS:
        raise ConfigError("未指定要测试的转录方案")
    if not stt_backend_ready(use, stt):
        missing = _stt_missing_fields(use, stt.get(use) or {})
        raise ConfigError(f"方案「{STT_LABELS[use]}」配置不完整：缺少 {', '.join(missing)}")
    prof = _profile_from_block(use, stt.get(use) or {})

    if prof.backend == "whisper":
        import httpx
        url = prof.base_url.rstrip("/") + "/health"
        with httpx.Client(timeout=8.0, trust_env=False) as cli:
            resp = cli.get(url)
            resp.raise_for_status()
            info = resp.json()
        return {"ok": True, "backend": "whisper", "detail": info}

    if prof.backend == "cloud":
        import httpx
        url = prof.base_url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {prof.api_key}"}
        with httpx.Client(timeout=15.0, trust_env=False) as cli:
            resp = cli.get(url, headers=headers)
            if resp.status_code == 404:
                return {"ok": True, "backend": "cloud", "detail": "endpoint reachable (no /models)"}
            resp.raise_for_status()
        return {"ok": True, "backend": "cloud", "model": prof.model}

    with funasr_env(prof):
        from scripts.audio_to_funasr import DashScopeConfig, OSSConfig
        DashScopeConfig.from_env()
        OSSConfig.from_env()
    return {"ok": True, "backend": "funasr", "model": prof.model}


def test_download_proxy(draft: dict[str, Any] | None = None) -> dict[str, Any]:
    """经代理访问 Google 探测端点，验证代理可用性。"""
    raw = _load_raw()
    block = (
        _merge_download_proxy(raw.get("download_proxy") or {}, draft)
        if isinstance(draft, dict)
        else (raw.get("download_proxy") or {})
    )
    if not block.get("enabled"):
        raise ConfigError("请先启用备用代理")
    url = str(block.get("url") or "").strip()
    if not url:
        raise ConfigError("请填写代理地址")
    validate_download_proxy_block({**block, "enabled": True})

    import httpx

    probe = "https://www.google.com/generate_204"
    t0 = time.perf_counter()
    try:
        with httpx.Client(proxy=url, trust_env=False, timeout=20.0, follow_redirects=True) as cli:
            resp = cli.get(probe)
            resp.raise_for_status()
    except Exception as e:
        raise ConfigError(f"代理不可用：{e}") from e
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return {
        "ok": True,
        "status_code": resp.status_code,
        "elapsed_ms": elapsed_ms,
        "probe": probe,
    }
