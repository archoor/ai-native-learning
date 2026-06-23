"""
文本块 → 学习用 segments。

与视频 segments 同构：[{index,start,end,source,target,kind}]，但 start/end 不再是
时间戳，而是「段落序号」（start=index, end=index+1）。这样：
- 学习面板的骨架/信息地图/高亮/自测全部零改动复用（只读 source 文本）；
- 信息地图的 start_sec 落在段落序号上，前端阅读器据此滚动定位段落。

过长段落按句子边界二次切分，避免单段过大影响阅读与标注粒度。
"""

from __future__ import annotations

import re

MAX_SEG_CHARS = 1400

_SENT_SPLIT = re.compile(r"(?<=[。．.!?！？])\s+|(?<=[。！？])")


def _split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts = [p for p in _SENT_SPLIT.split(text) if p and p.strip()]
    out: list[str] = []
    buf = ""
    for p in parts:
        if buf and len(buf) + len(p) > max_chars:
            out.append(buf.strip())
            buf = p
        else:
            buf = (buf + p) if not buf else (buf + " " + p)
    if buf.strip():
        out.append(buf.strip())
    # 仍可能存在无标点的超长块：硬切
    final: list[str] = []
    for seg in out:
        if len(seg) <= max_chars:
            final.append(seg)
        else:
            for i in range(0, len(seg), max_chars):
                final.append(seg[i:i + max_chars])
    return final or [text[:max_chars]]


def blocks_to_segments(blocks: list[dict], max_chars: int = MAX_SEG_CHARS) -> list[dict]:
    segs: list[dict] = []
    idx = 0
    seen: set[str] = set()  # 去重：部分抽取器会把长段落重复输出
    for b in blocks:
        text = (b.get("text") or "").strip()
        if not text:
            continue
        # 仅对较长文本去重，避免误删合法的短重复（如列表项"是/否"）
        if len(text) > 40:
            if text in seen:
                continue
            seen.add(text)
        kind = b.get("kind", "p")
        # 代码块不二次切分（保持完整可读）
        chunks = [text] if kind == "code" else _split_long(text, max_chars)
        for chunk in chunks:
            segs.append({
                "index": idx,
                "start": float(idx),
                "end": float(idx + 1),
                "source": chunk,
                "target": "",
                "kind": kind,
            })
            idx += 1
    return segs
