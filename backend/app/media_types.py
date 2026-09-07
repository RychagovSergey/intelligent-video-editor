"""Определение типа медиафайла по расширению (ТЗ п. 3.1)."""
from __future__ import annotations

from pathlib import Path

from .models import MediaType
from .paths import legacy_meta_path_for, meta_path_for  # noqa: F401  (реэкспорт)

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".mpg", ".mpeg", ".wmv", ".flv", ".mts", ".m2ts"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".gif"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".aiff", ".aif"}

META_SUFFIX = ".meta.json"  # Р-1: сайдкар называется <имя_файла>.meta.json

#: Каталоги, внутрь которых не заходим.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".Trash", "$RECYCLE.BIN"}
#: Пакеты macOS, которые выглядят как папки.
SKIP_SUFFIXES = {".photoslibrary", ".fcpbundle", ".app", ".imovielibrary", ".tvlibrary"}


def media_type(path: Path) -> MediaType | None:
    ext = path.suffix.lower()
    if ext in VIDEO_EXT:
        return MediaType.video
    if ext in IMAGE_EXT:
        return MediaType.image
    if ext in AUDIO_EXT:
        return MediaType.audio
    return None


def should_skip_dir(path: Path) -> bool:
    name = path.name
    return (
        name in SKIP_DIRS
        or name.startswith(".")
        or path.suffix.lower() in SKIP_SUFFIXES
    )
