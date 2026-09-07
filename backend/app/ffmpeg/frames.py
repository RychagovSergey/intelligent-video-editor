"""Извлечение кадров для анализа моделью (ТЗ пп. 3.2, 4.3).

Режим по умолчанию — выборка по времени (`fps=1/N`, решение Р-7): число кадров
предсказуемо и не зависит от частоты исходника. Режим «каждый N-й кадр» из п. 4.3 ТЗ
доступен как альтернатива. Устаревший `-vsync vfr` заменён на `-fps_mode vfr`.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from .probe import probe
from .resize import scale_filter
from .runner import BinaryMissing, run

log = logging.getLogger(__name__)

MAX_FRAMES_DEFAULT = 12


@dataclass
class Frame:
    index: int
    time: float
    path: Path


@contextmanager
def extract_frames(
    video: Path,
    *,
    every_seconds: float | None = None,
    every_nth_frame: int | None = None,
    max_frames: int = MAX_FRAMES_DEFAULT,
) -> Iterator[list[Frame]]:
    """Извлекает кадры во временную папку и удаляет её на выходе из контекста."""
    tmp = Path(tempfile.mkdtemp(prefix="ive_frames_"))
    try:
        yield _extract(video, tmp, every_seconds, every_nth_frame, max_frames)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _extract(
    video: Path,
    out_dir: Path,
    every_seconds: float | None,
    every_nth_frame: int | None,
    max_frames: int,
) -> list[Frame]:
    info = probe(video)
    if not info.ok:
        log.warning("Кадры не извлечены, файл не читается: %s (%s)", video, info.error)
        return []

    duration = info.duration or 0.0
    if every_nth_frame:
        select = f"select='not(mod(n\\,{every_nth_frame}))'"
        fps = info.fps or 25.0
        step = every_nth_frame / fps
    else:
        step = every_seconds or settings.frame_sample_seconds
        # Короткое видео иначе даст один кадр — растягиваем шаг под длительность.
        if duration and duration / step > max_frames:
            step = duration / max_frames
        select = f"fps=1/{step:.4f}"

    args = [
        "-hide_banner", "-loglevel", "error",
        "-i", str(video),
        "-vf", f"{select},{scale_filter()}",
        "-fps_mode", "vfr",
        "-frames:v", str(max_frames),
        "-q:v", "3",
        "-y", str(out_dir / "frame_%04d.jpg"),
    ]

    try:
        result = run("ffmpeg", args, timeout=300.0)
    except BinaryMissing as exc:
        log.warning("Кадры не извлечены: %s", exc)
        return []

    if not result.ok:
        log.warning("ffmpeg не смог извлечь кадры из %s: %s", video, result.stderr.strip()[:200])
        return []

    frames = []
    for i, path in enumerate(sorted(out_dir.glob("frame_*.jpg"))):
        frames.append(Frame(index=i, time=round(i * step, 3), path=path))
    return frames
