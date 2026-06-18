"""
AI原生学习（ai-native-learning）启动入口。

用法：
  uv run python ai_native_learning/run.py            # 默认 127.0.0.1:8810，自动打开浏览器
  uv run python ai_native_learning/run.py --port 9000
  uv run python ai_native_learning/run.py --no-open  # 不自动打开浏览器
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from pathlib import Path

# 注入项目根，使后端可 import scripts.* 与 subtitle_player.*
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _configure_stdio_utf8() -> None:
    """Windows 下将 stdout/stderr 设为 UTF-8，避免 yt-dlp 等输出触发 GBK 编码错误。"""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _silence_proactor_connection_reset() -> None:
    """
    抑制 Windows asyncio Proactor 在客户端中断连接时抛出的无害异常。

    <video> 标签播放/拖动时会频繁中断 Range 请求，CPython 会在
    `_ProactorBasePipeTransport._call_connection_lost` 里抛出
    ConnectionResetError([WinError 10054])，仅是噪音、不影响功能（206 仍正常返回）。
    """
    if sys.platform != "win32":
        return
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport
    except Exception:
        return

    _orig = _ProactorBasePipeTransport._call_connection_lost

    def _patched(self, exc):  # type: ignore[no-untyped-def]
        try:
            _orig(self, exc)
        except (ConnectionResetError, ConnectionAbortedError):
            pass

    _ProactorBasePipeTransport._call_connection_lost = _patched


def _open_browser_later(url: str, delay: float = 1.2) -> None:
    def _open():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()


def main() -> int:
    ap = argparse.ArgumentParser(description="AI原生学习（ai-native-learning）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8810)
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    _configure_stdio_utf8()
    import uvicorn

    _silence_proactor_connection_reset()

    from ai_native_learning.backend.app import app

    url = f"http://{args.host}:{args.port}"
    print(f"AI原生学习启动中 … 打开 {url}")
    if not args.no_open:
        _open_browser_later(url)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
