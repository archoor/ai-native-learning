"""
图片 OCR：本地 RapidOCR 出带 bbox 的文本行（逐行成块，bbox 精确到行）。

对外：
- image_to_blocks(path) -> (title, blocks)
  blocks: [{"text": str, "kind": "h1|h2|p", "bbox": [x, y, w, h]}]，bbox 归一化(0–1)。

设计理由：
- 本地 OCR（rapidocr-onnxruntime）按行返回精确包围框，离线可用、坐标可靠——
  这是"点文字定位到原图"功能的前提（视觉大模型给不出可靠坐标）。
- **一行 OCR = 一个块 = 一个紧致 bbox**：保证右侧「全文」每一条与原图上一个框
  严格一一对应、定位精准（不做段落合并，避免一个大框跨多行、点哪条都框一整段）。
- 标题按行高启发式判定（h1/h2），仅用于阅读层级，不影响定位。
- 信息地图 / 骨架仍由下游 LLM 基于全文文本生成。

依赖按需延迟导入，缺失时给出可读报错。
"""

from __future__ import annotations

import threading
from pathlib import Path
from statistics import median

# 标题判定：行高 ≥ 中位行高该倍数且文本较短
_HEADING_RATIO = 1.35
_H1_RATIO = 1.7
_HEADING_MAX_CHARS = 40

_engine = None
_engine_lock = threading.Lock()


def _get_engine():
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                try:
                    from rapidocr_onnxruntime import RapidOCR
                except ImportError:
                    raise RuntimeError(
                        "缺少 rapidocr-onnxruntime，请先安装：uv add rapidocr-onnxruntime"
                    ) from None
                _engine = RapidOCR()
    return _engine


def _image_size(p: Path) -> tuple[int, int]:
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("缺少 Pillow，请先安装：uv add pillow") from None
    with Image.open(p) as im:
        return im.size  # (w, h)


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _norm_bbox(x0: float, y0: float, x1: float, y1: float, w: int, h: int) -> list[float]:
    w = max(1, w)
    h = max(1, h)
    nx = _clamp01(x0 / w)
    ny = _clamp01(y0 / h)
    nw = _clamp01((x1 - x0) / w)
    nh = _clamp01((y1 - y0) / h)
    return [round(nx, 5), round(ny, 5), round(nw, 5), round(nh, 5)]


def image_to_blocks(path: str | Path) -> tuple[str, list[dict]]:
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise RuntimeError(f"图片文件不存在：{p}")

    engine = _get_engine()
    try:
        result, _ = engine(str(p))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"图片 OCR 失败：{type(e).__name__}: {e}") from None

    if not result:
        raise RuntimeError("未识别到文字（可能是纯图像、分辨率过低或文字过小）")

    w, h = _image_size(p)

    lines: list[dict] = []
    for item in result:
        # rapidocr 每项：[box(4 点), text, score]
        box, text = item[0], item[1]
        text = (text or "").strip()
        if not text:
            continue
        xs = [float(pt[0]) for pt in box]
        ys = [float(pt[1]) for pt in box]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        lines.append({
            "text": text,
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "h": max(1.0, y1 - y0),
        })

    if not lines:
        raise RuntimeError("未识别到文字（可能是纯图像、分辨率过低或文字过小）")

    # 阅读顺序：自上而下、同行自左而右
    lines.sort(key=lambda l: (l["y0"], l["x0"]))
    mh = median([l["h"] for l in lines]) or 1.0

    # 逐行成块：每行一个紧致 bbox，与右侧「全文」一一对应
    blocks: list[dict] = []
    for l in lines:
        is_heading = l["h"] >= _HEADING_RATIO * mh and len(l["text"]) <= _HEADING_MAX_CHARS
        if is_heading:
            kind = "h1" if l["h"] >= _H1_RATIO * mh else "h2"
        else:
            kind = "p"
        blocks.append({
            "text": l["text"], "kind": kind,
            "bbox": _norm_bbox(l["x0"], l["y0"], l["x1"], l["y1"], w, h),
        })

    blocks = [b for b in blocks if b["text"]]
    if not blocks:
        raise RuntimeError("未识别到文字（可能是纯图像、分辨率过低或文字过小）")

    title = next((b["text"] for b in blocks if b["kind"].startswith("h")), "")
    if not title:
        title = blocks[0]["text"][:_HEADING_MAX_CHARS]
    return title.strip(), blocks
