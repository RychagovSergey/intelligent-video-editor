"""Текстовый слой поверх видео — рендерится в PNG через Pillow, не через ffmpeg.

Системный ffmpeg собран без libfreetype/libfontconfig (`ffmpeg -filters` не находит
`drawtext`), так что накладывать текст средствами самого ffmpeg нельзя. Pillow рисует
текст в прозрачный PNG размера кадра, а в `compile.py` он подмешивается фильтром
`overlay`. Шрифт — системный macOS (проект целится только в macOS, Р-4).
"""
from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
STROKE_WIDTH = 2   # чёрная обводка — читаемость текста на любом фоне видео

_TOP_MARGIN = 0.08
_BOTTOM_MARGIN = 0.85


def render_text_png(
    text: str, *, width: int, height: int, font_size: int, position: str, path: Path
) -> Path:
    """Рисует `text` на прозрачном холсте размера кадра, сохраняет в `path`."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(FONT_PATH, font_size)
    except OSError as exc:
        log.warning("Шрифт %s не найден (%s), беру встроенный", FONT_PATH, exc)
        font = ImageFont.load_default(size=font_size)

    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=STROKE_WIDTH)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (width - text_w) / 2 - bbox[0]

    if position == "top":
        y = height * _TOP_MARGIN
    elif position == "center":
        y = (height - text_h) / 2 - bbox[1]
    else:  # bottom
        y = height * _BOTTOM_MARGIN

    draw.text(
        (x, y), text, font=font, fill=(255, 255, 255, 255),
        stroke_width=STROKE_WIDTH, stroke_fill=(0, 0, 0, 220),
    )
    img.save(path)
    return path
