"""Подготовка изображений для VL-модели.

Модель не выигрывает от полноразмерных кадров, а время и память растут: снимок 1536×1024
и кадр 4K приводятся к коробке `MODEL_IMAGE_MAX_SIDE` (640 по большей стороне).
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..config import settings
from .runner import BinaryMissing, run

log = logging.getLogger(__name__)


def scale_filter(max_side: int | None = None) -> str:
    """Вписывает кадр в квадрат max_side×max_side, сохраняя пропорции и не увеличивая мелкие."""
    side = max_side or settings.model_image_max_side
    return f"scale=w={side}:h={side}:force_original_aspect_ratio=decrease"


def downscale(src: Path, dst: Path, max_side: int | None = None) -> Path | None:
    """Делает уменьшенную JPEG-копию для отправки в модель."""
    args = [
        "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-frames:v", "1",
        "-vf", scale_filter(max_side),
        "-q:v", "3",
        "-y", str(dst),
    ]
    try:
        result = run("ffmpeg", args, timeout=60.0)
    except BinaryMissing as exc:
        log.warning("Не удалось уменьшить %s: %s", src, exc)
        return None
    if not result.ok or not dst.exists():
        log.warning("ffmpeg не смог уменьшить %s: %s", src, result.stderr.strip()[:200])
        return None
    return dst


@contextmanager
def downscaled(src: Path, max_side: int | None = None) -> Iterator[Path]:
    """Отдаёт уменьшенную копию во временной папке; при неудаче — исходный файл."""
    tmp = Path(tempfile.mkdtemp(prefix="ive_scaled_"))
    try:
        out = downscale(src, tmp / "image.jpg", max_side)
        yield out or src
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
