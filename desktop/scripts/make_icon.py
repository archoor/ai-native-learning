"""生成 AI原生学习 桌面图标（多分辨率 .ico）。

与 frontend/style.css `.brand .logo` 一致：
  linear-gradient(135deg, #0a84ff, #34c759) + 白色粗体 AI
用法（仓库根）：
    uv run python ai_native_learning/desktop/scripts/make_icon.py
产物：ai_native_learning/desktop/build/icon.ico、icon_preview.png
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "build"
OUT = OUT_DIR / "app-icon.ico"
LEGACY_OUT = OUT_DIR / "icon.ico"
PREVIEW = OUT_DIR / "icon_preview.png"

# 与 style.css .brand .logo 一致
GRAD_START = (10, 132, 255)   # #0a84ff
GRAD_END = (52, 199, 89)      # #34c759
WHITE = (255, 255, 255, 255)
RADIUS_RATIO = 7 / 22         # border-radius: 7px on 22px box
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

_FONT_CANDIDATES = (
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/calibrib.ttf",
)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        p = Path(path)
        if p.exists():
            return ImageFont.truetype(str(p), size=size)
    return ImageFont.load_default()


def _build_background(size: int) -> Image.Image:
    """135deg 蓝→绿渐变 + 圆角方形。"""
    angle = math.radians(135)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    denom = size * (abs(cos_a) + abs(sin_a))

    grad = Image.new("RGB", (size, size))
    px = grad.load()
    for y in range(size):
        for x in range(size):
            t = (x * cos_a + y * sin_a) / max(denom, 1)
            t = max(0.0, min(1.0, t))
            px[x, y] = (
                int(_lerp(GRAD_START[0], GRAD_END[0], t)),
                int(_lerp(GRAD_START[1], GRAD_END[1], t)),
                int(_lerp(GRAD_START[2], GRAD_END[2], t)),
            )

    mask = Image.new("L", (size, size), 0)
    radius = max(2, int(size * RADIUS_RATIO))
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=radius, fill=255
    )

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    return img


def _draw_ai_text(img: Image.Image, *, small: bool = False) -> None:
    size = img.width
    draw = ImageDraw.Draw(img)
    font_size = max(8, int(size * (0.52 if small else 0.46)))
    font = _load_font(font_size)
    text = "AI"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - tw) / 2 - bbox[0]
    y = (size - th) / 2 - bbox[1] - size * 0.02
    draw.text((x, y), text, font=font, fill=WHITE)


def _render_icon(target: int) -> Image.Image:
    small = target <= 32
    ss = target * 8 if small else target * 4
    img = _build_background(ss)
    _draw_ai_text(img, small=small)
    out = img.resize((target, target), Image.LANCZOS)
    if small:
        out = out.filter(ImageFilter.UnsharpMask(radius=0.6, percent=150, threshold=2))
    return out


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    frames = [_render_icon(s) for s in ICO_SIZES]
    largest = frames[-1]
    largest.save(
        OUT,
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=frames[:-1],
    )
    # 同步旧路径，避免其它引用失效
    import shutil
    shutil.copy2(OUT, LEGACY_OUT)
    shutil.copy2(OUT, ROOT / "app-icon.ico")
    frames[-1].save(PREVIEW)
    print(f"已生成: {OUT} ({OUT.stat().st_size // 1024} KB, {len(ICO_SIZES)} 档尺寸)")
    print(f"同步: {LEGACY_OUT}, {ROOT / 'app-icon.ico'}")
    print(f"预览图: {PREVIEW}")


if __name__ == "__main__":
    main()
