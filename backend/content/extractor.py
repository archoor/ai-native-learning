"""
来源识别 + 正文抽取：把网页链接、txt/md/pdf 文件抽成结构化文本块。

对外：
- TEXT_EXTS                可作为文本学习资料的扩展名集合
- is_video_url(url)        URL 是否指向已知视频站（决定走视频还是文章流程）
- is_text_file(path)       本地路径是否为受支持的文本文件
- extract_url(url, ...)    抓取网页 → (title, blocks)
- extract_file(path)       读取本地文件 → (title, blocks)

block 结构：{"text": str, "kind": "h1|h2|h3|p|li|quote|code"}，供 segmenter 切段、
前端阅读器按结构渲染。抽取库（trafilatura / pymupdf）按需延迟导入，缺失时给出可读报错。
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

TEXT_EXTS = {".txt", ".md", ".markdown", ".pdf"}

# 已知视频/音频站点：命中则按视频流程（yt-dlp 下载转录），否则按网页文章抽取正文。
_VIDEO_HOSTS = (
    "youtube.com", "youtu.be", "bilibili.com", "b23.tv",
    "vimeo.com", "youku.com", "iqiyi.com", "v.qq.com", "ixigua.com",
    "douyin.com", "tiktok.com", "twitch.tv", "dailymotion.com",
    "ted.com", "coursera.org", "udemy.com", "netflix.com",
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def is_video_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    host = host[4:] if host.startswith("www.") else host
    return any(host == h or host.endswith("." + h) for h in _VIDEO_HOSTS)


def is_text_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in TEXT_EXTS


# ── 网页 ──────────────────────────────────────────────────────────────────────

def _fetch_html(url: str, fallback_proxy: str | None = None) -> str:
    import requests

    def _get(proxies: dict | None) -> str:
        r = requests.get(url, headers=_HEADERS, timeout=25, proxies=proxies)
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding or "utf-8"
        return r.text

    try:
        return _get(None)
    except Exception:
        if fallback_proxy:
            return _get({"http": fallback_proxy, "https": fallback_proxy})
        raise


def extract_url(url: str, fallback_proxy: str | None = None) -> tuple[str, list[dict]]:
    try:
        import trafilatura
    except ImportError as e:  # noqa: F841
        raise RuntimeError("缺少 trafilatura，请先安装：uv add trafilatura") from None

    html = _fetch_html(url, fallback_proxy)

    title = ""
    try:
        meta = trafilatura.extract_metadata(html)
        if meta and getattr(meta, "title", None):
            title = (meta.title or "").strip()
    except Exception:
        pass

    markdown = trafilatura.extract(
        html,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        include_images=False,
        favor_precision=True,
    )
    if not markdown or not markdown.strip():
        raise RuntimeError("未能从网页中抽取到正文（可能需要登录或为动态渲染页面）")

    blocks = _markdown_to_blocks(markdown)
    if not title:
        title = next((b["text"] for b in blocks if b["kind"].startswith("h")), "") or url
    return title.strip(), blocks


# ── 本地文件 ──────────────────────────────────────────────────────────────────

def extract_file(path: str | Path) -> tuple[str, list[dict]]:
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise RuntimeError(f"文件不存在：{p}")
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        text = _read_pdf(p)
        blocks = _plain_to_blocks(text)
    elif suffix in (".md", ".markdown"):
        blocks = _markdown_to_blocks(_read_text(p))
    elif suffix == ".txt":
        blocks = _plain_to_blocks(_read_text(p))
    else:
        raise RuntimeError(f"不支持的文本类型：{suffix}")
    if not blocks:
        raise RuntimeError("文件中没有可提取的文本内容")
    title = next((b["text"] for b in blocks if b["kind"].startswith("h")), "") or p.stem
    return title.strip(), blocks


def _read_text(p: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return p.read_text(encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return p.read_text(encoding="utf-8", errors="replace")


def _read_pdf(p: Path) -> str:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise RuntimeError("缺少 PyMuPDF，请先安装：uv add pymupdf") from None
    parts: list[str] = []
    with fitz.open(str(p)) as doc:
        for page in doc:
            parts.append(page.get_text("text"))
    text = "\n\n".join(parts)
    if not text.strip():
        raise RuntimeError("PDF 没有可提取的文本层（可能是扫描图片，暂不支持 OCR）")
    return text


# ── 解析为文本块 ──────────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_FENCE_RE = re.compile(r"^```")


def _markdown_to_blocks(md: str) -> list[dict]:
    """把 markdown 文本解析为有序文本块（标题/段落/列表项/引用/代码）。"""
    blocks: list[dict] = []
    para: list[str] = []
    in_code = False
    code: list[str] = []

    def flush_para() -> None:
        if para:
            text = " ".join(x.strip() for x in para).strip()
            if text:
                blocks.append({"text": text, "kind": "p"})
            para.clear()

    for raw in md.splitlines():
        line = raw.rstrip()
        if _FENCE_RE.match(line.strip()):
            if in_code:
                if code:
                    blocks.append({"text": "\n".join(code), "kind": "code"})
                code.clear()
                in_code = False
            else:
                flush_para()
                in_code = True
            continue
        if in_code:
            code.append(raw)
            continue

        if not line.strip():
            flush_para()
            continue

        m = _HEADING_RE.match(line)
        if m:
            flush_para()
            level = min(len(m.group(1)), 3)
            blocks.append({"text": m.group(2).strip(), "kind": f"h{level}"})
            continue

        m = _LIST_RE.match(line)
        if m:
            flush_para()
            blocks.append({"text": m.group(3).strip(), "kind": "li"})
            continue

        m = _QUOTE_RE.match(line)
        if m:
            flush_para()
            blocks.append({"text": m.group(1).strip(), "kind": "quote"})
            continue

        para.append(line)

    flush_para()
    if in_code and code:
        blocks.append({"text": "\n".join(code), "kind": "code"})
    return [b for b in blocks if b["text"]]


def _plain_to_blocks(text: str) -> list[dict]:
    """纯文本：按空行切段；过短独立成行的首段视为标题。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    chunks = re.split(r"\n\s*\n", text)
    blocks: list[dict] = []
    for i, chunk in enumerate(chunks):
        t = " ".join(x.strip() for x in chunk.splitlines() if x.strip()).strip()
        if not t:
            continue
        kind = "h1" if (i == 0 and len(t) <= 60 and "\n" not in chunk.strip()) else "p"
        blocks.append({"text": t, "kind": kind})
    return blocks
