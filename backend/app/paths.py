"""Расположение кеша приложения.

Весь производный материал хранится рядом с медиатекой, а не внутри репозитория:
`<корень хранилища>/data/` содержит базу индекса, метаданные, миниатюры и прокси.
Так библиотека самодостаточна — её можно перенести вместе с разобранными данными.
Путь переопределяется переменной `DATA_DIR` в `.env`.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .config import expand, settings

CACHE_DIR_NAME = "data"
META_SUFFIX = ".meta.json"


def cache_dir() -> Path:
    """Папка кеша: из `DATA_DIR` либо `data` внутри первого корня хранилища."""
    if settings.data_dir_override:
        return expand(settings.data_dir_override)
    roots = settings.default_media_roots
    if roots:
        return roots[0] / CACHE_DIR_NAME
    # Хранилище ещё не выбрано — держим кеш рядом с проектом, чтобы приложение стартовало.
    return settings.project_root / CACHE_DIR_NAME


def meta_dir() -> Path:
    return cache_dir() / "meta"


def thumbs_dir() -> Path:
    return cache_dir() / "thumbs"


def proxy_dir() -> Path:
    return cache_dir() / "proxy"


def waveforms_dir() -> Path:
    return cache_dir() / "waveforms"


def db_path() -> Path:
    return cache_dir() / "app.db"


def is_inside_cache(path: Path) -> bool:
    """Кеш лежит внутри хранилища, поэтому сканер обязан его пропускать."""
    try:
        path.resolve().relative_to(cache_dir().resolve())
    except (ValueError, OSError):
        return False
    return True


def meta_path_for(media_path: Path | str) -> Path:
    """`<корень>/clips/a.mp4` → `<кеш>/meta/clips/a.mp4.meta.json`.

    Структура папок повторяет хранилище, поэтому в кеше видно, к чему относится файл.
    Файлы вне известных корней складываются в `_abs` по абсолютному пути.
    """
    path = Path(media_path)
    for root in settings.default_media_roots:
        try:
            relative = path.resolve().relative_to(root)
        except (ValueError, OSError):
            continue
        return meta_dir() / relative.parent / f"{path.name}{META_SUFFIX}"

    absolute = Path(str(path).lstrip("/"))
    return meta_dir() / "_abs" / absolute.parent / f"{path.name}{META_SUFFIX}"


def legacy_meta_path_for(media_path: Path | str) -> Path:
    """Прежняя схема — сайдкар рядом с файлом. Читаем для совместимости, но не пишем."""
    path = Path(media_path)
    return path.with_name(path.name + META_SUFFIX)


def thumb_path(media_path: Path, size: int, mtime: float, width: int) -> Path:
    key = f"{media_path}|{size}|{mtime}|{width}".encode()
    return thumbs_dir() / f"{hashlib.sha1(key).hexdigest()}.jpg"


#: Меняется при правке алгоритма построения waveform — так старый кеш не выдаётся молча.
WAVEFORM_CACHE_VERSION = 2


def waveform_path(media_path: Path, mtime: float, points: int) -> Path:
    key = f"{media_path}|{mtime}|{points}|v{WAVEFORM_CACHE_VERSION}".encode()
    return waveforms_dir() / f"{hashlib.sha1(key).hexdigest()}.json"
