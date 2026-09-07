"""Огибающая амплитуды аудиофайла — для отрисовки waveform на таймлайне.

Тот же подход, что и в `audio_events.py`: чистый ffmpeg (`astats`), без новых
Python-зависимостей. Результат кешируется на диске (Р-15), как миниатюры в `thumbs.py`.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..paths import waveform_path, waveforms_dir
from .ametadata import parse_metadata_series
from .runner import BinaryMissing, run

log = logging.getLogger(__name__)

MIN_POINTS = 10
MAX_POINTS = 2000
DEFAULT_POINTS = 800
SAMPLE_RATE = 8000       # с запасом хватает для огибающей — это не звук, а картинка
TIMEOUT = 180.0
SILENCE_DB = -50.0       # ниже этого уровня RMS считаем амплитуду нулевой


def ensure_waveform(path: Path, mtime: float, duration: float, points: int = DEFAULT_POINTS) -> list[float]:
    """Пик амплитуды (0..1) на каждую из `points` равных долей файла. Кеш на диске по mtime."""
    points = max(MIN_POINTS, min(MAX_POINTS, points))
    cache_file = waveform_path(path, mtime, points)
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass   # кеш повреждён — пересчитаем

    values = extract_waveform(str(path), duration=duration, points=points)

    waveforms_dir().mkdir(parents=True, exist_ok=True)
    try:
        cache_file.write_text(json.dumps(values))
    except OSError as exc:
        log.warning("Waveform не закеширован для %s: %s", path, exc)
    return values


def extract_waveform(path: str, *, duration: float, points: int) -> list[float]:
    if duration <= 0:
        return [0.0] * points

    window = duration / points
    samples_per_window = max(1, int(SAMPLE_RATE * window))
    try:
        result = run(
            "ffmpeg",
            ["-hide_banner", "-i", path, "-af",
             f"aresample={SAMPLE_RATE},asetnsamples=n={samples_per_window},"
             # RMS, а не пиковая громкость: у смастеренных треков пик почти всюду
             # упирается в 0 дБ (лимитер), и waveform выходит одной плоской «доской».
             # RMS честно показывает реальную динамику громкости по треку.
             "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
             "-f", "null", "-"],
            timeout=TIMEOUT,
        )
    except BinaryMissing as exc:
        log.warning("Waveform не построен: %s", exc)
        return [0.0] * points

    if not result.ok:
        log.warning("Waveform: ffmpeg завершился с ошибкой для %s", path)
        return [0.0] * points

    series = parse_metadata_series(result.stdout, "lavfi.astats.Overall.RMS_level")
    return _bucket(series, duration, points)


def _bucket(series: list[tuple[float, float]], duration: float, points: int) -> list[float]:
    buckets = [0.0] * points
    step = duration / points
    for time, db in series:
        index = min(points - 1, max(0, int(time / step)))
        amplitude = _db_to_linear(db)
        if amplitude > buckets[index]:
            buckets[index] = amplitude
    return _normalize(buckets)


def _normalize(buckets: list[float]) -> list[float]:
    """Растягивает по самому громкому месту трека — иначе тихие/RMS-скромные треки
    рисуются мелкими барами даже там, где в самом треке звук уже максимально громкий."""
    peak = max(buckets, default=0.0)
    if peak <= 0.0:
        return buckets
    return [round(min(1.0, v / peak), 4) for v in buckets]


def _db_to_linear(db: float) -> float:
    if db <= SILENCE_DB:
        return 0.0
    return round(min(1.0, 10 ** (db / 20.0)), 4)
