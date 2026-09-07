"""Темп и тактовая сетка аудиофайла — для монтажа синхронно с музыкой.

Единственное место в проекте, где вместо системного ffmpeg используется Python-
библиотека (librosa): поиск темпа — автокорреляция по onset-огибающей, а не то, что
считает сам ffmpeg (обсуждалось и согласовано отдельно, см. PLAN.md). Модуль называется
`bpm.py`, а не лежит в `backend/app/audio/`, чтобы держать всю аудио-аналитику рядом —
как `audio_events.py` и `waveform.py`.

Даунбиты (первая доля такта) честный трекинг требует отдельной обученной модели
(например, madmom) — здесь это грубое приближение «каждая 4-я доля», подходящее для
почти всей танцевальной музыки (4/4), но не гарантированно верное на сложных размерах.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

BEATS_PER_BAR = 4   # предположение 4/4 — доля методичного даунбит-трекинга не входит в объём


@dataclass
class BeatGrid:
    bpm: float | None = None
    beat_times: list[float] = field(default_factory=list)
    downbeats: list[float] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def detect_beat_grid(path: str) -> BeatGrid:
    """BPM, доли (`beat_times`) и приближённые начала тактов (`downbeats`), в секундах."""
    try:
        import librosa
    except ImportError:
        return BeatGrid(error="librosa не установлен — добавьте в backend/requirements.txt")

    try:
        y, sr = librosa.load(path, sr=22050, mono=True)
    except Exception as exc:  # noqa: BLE001 — librosa/audioread на битых файлах кидают разное
        return BeatGrid(error=f"Не удалось прочитать аудио: {exc}")

    if y.size == 0:
        return BeatGrid(error="Файл пуст или нечитаем")

    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    bpm = float(_scalar(tempo))
    beat_times = [round(float(t), 3) for t in librosa.frames_to_time(beat_frames, sr=sr)]
    downbeats = beat_times[::BEATS_PER_BAR]

    return BeatGrid(bpm=round(bpm, 1), beat_times=beat_times, downbeats=downbeats)


def _scalar(value) -> float:
    """`beat_track` отдаёт темп то числом, то одноэлементным numpy-массивом — приводим к float."""
    try:
        return float(value[0])
    except TypeError:
        return float(value)
