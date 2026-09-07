"""Миниатюры для списка файлов (ТЗ п. 3.7: список медиафайлов с иконками)."""
from __future__ import annotations

import logging
from pathlib import Path

from ..models import MediaType
from ..paths import thumb_path as cache_thumb_path
from ..paths import thumbs_dir
from .probe import probe
from .runner import BinaryMissing, run

log = logging.getLogger(__name__)

THUMB_WIDTH = 320
VIEW_WIDTH = 1280       # для просмотра в центральной панели


def thumb_path(path: Path, size: int, mtime: float, width: int = THUMB_WIDTH) -> Path:
    """Ключ кеша учитывает mtime и размер — пересозданный файл получит новую миниатюру."""
    return cache_thumb_path(path, size, mtime, width)


def ensure_thumbnail(
    path: Path, kind: MediaType, size: int, mtime: float, width: int = THUMB_WIDTH
) -> Path | None:
    """Путь к миниатюре, создаётся при необходимости.

    Для аудио миниатюра есть, только если внутри файла лежит обложка.
    """
    target = thumb_path(path, size, mtime, width)
    if target.exists():
        return target
    if not path.exists():
        return None

    thumbs_dir().mkdir(parents=True, exist_ok=True)
    args: list[str] = ["-hide_banner", "-loglevel", "error"]

    if kind == MediaType.audio:
        info = probe(path)
        if not info.ok or not info.has_cover_art:
            return None
    elif kind == MediaType.video:
        # Кадр из начала, но не самый первый: первые кадры часто чёрные.
        info = probe(path)
        if not info.ok:
            return None
        seek = min(1.0, (info.duration or 1.0) / 10)
        args += ["-ss", f"{seek:.3f}"]

    args += [
        "-i", str(path),
        "-frames:v", "1",
        "-vf", f"scale={width}:-2:force_original_aspect_ratio=decrease",
        "-q:v", "4",
        "-y", str(target),
    ]

    try:
        result = run("ffmpeg", args, timeout=30.0)
    except BinaryMissing as exc:
        log.warning("Миниатюра не создана: %s", exc)
        return None

    if not result.ok or not target.exists():
        log.warning("Не удалось создать миниатюру для %s: %s", path, result.stderr.strip()[:200])
        target.unlink(missing_ok=True)
        return None
    return target
