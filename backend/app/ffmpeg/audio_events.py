"""Тишина и всплески громкости в аудиофайле — чтобы агент мог резать видео под музыку.

Никаких новых зависимостей: всё через фильтры самого ffmpeg (Р-5), два decode-only
прохода без перекодирования и без записи файла на диск (`-f null -`).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .ametadata import parse_metadata_series
from .runner import BinaryMissing, run

log = logging.getLogger(__name__)

WINDOW_SECONDS = 0.5     # шаг окна для огибающей громкости
#: Порог пика считается от собственного разброса громкости трека, а не фиксированным
#: числом децибел. Прежние «6 dB над медианой» для музыки недостижимы: у плотно
#: отмастеренного трека весь разброс RMS укладывается в 1–3 dB, и `peaks` всегда
#: возвращался пустым — инструмент был бесполезен ровно на том материале, ради
#: которого писался. MAD (медиана отклонений) даёт масштаб самого трека, а пол
#: PEAK_MIN_LIFT_DB не даёт принять за акцент шум измерения на ровном треке.
PEAK_MIN_LIFT_DB = 1.0   # подъём над медианой, ниже которого это ещё не акцент
PEAK_MAD_FACTOR = 3.0    # во сколько раз пик должен превышать типичный разброс трека
TIMEOUT = 180.0          # decode-only проход по музыкальному треку — быстрее реального времени, но не мгновенно

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")


@dataclass
class AudioEvents:
    silences: list[dict] = field(default_factory=list)
    peaks: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class _FfmpegFailed(RuntimeError):
    """Один из двух проходов ffmpeg завершился ошибкой (битый файл, нечитаемый кодек и т. п.)."""


def detect_audio_events(
    path: str,
    *,
    duration: float | None = None,
    silence_db: float = -30.0,
    min_silence: float = 0.3,
) -> AudioEvents:
    """Паузы (`silences`) и резкие всплески громкости (`peaks`), в секундах от начала файла."""
    try:
        silences = _detect_silences(path, silence_db=silence_db, min_silence=min_silence, duration=duration)
        peaks = _detect_peaks(path)
    except BinaryMissing as exc:
        return AudioEvents(error=str(exc))
    except _FfmpegFailed as exc:
        return AudioEvents(error=str(exc))
    return AudioEvents(silences=silences, peaks=peaks)


def _detect_silences(
    path: str, *, silence_db: float, min_silence: float, duration: float | None
) -> list[dict]:
    result = run(
        "ffmpeg",
        ["-hide_banner", "-i", path, "-af", f"silencedetect=noise={silence_db}dB:d={min_silence}",
         "-f", "null", "-"],
        timeout=TIMEOUT,
    )
    if not result.ok:
        message = [line for line in result.stderr.splitlines() if line.strip()]
        raise _FfmpegFailed(message[-1] if message else "ffmpeg завершился с ошибкой")
    starts = [float(m.group(1)) for m in _SILENCE_START_RE.finditer(result.stderr)]
    ends = [float(m.group(1)) for m in _SILENCE_END_RE.finditer(result.stderr)]

    silences = []
    for i, start in enumerate(starts):
        end = ends[i] if i < len(ends) else duration
        if end is None:
            continue   # файл кончился в тишине, а длительность неизвестна — интервал не закрыть
        silences.append({"start": round(start, 2), "end": round(end, 2)})
    return silences


def _detect_peaks(path: str) -> list[dict]:
    sample_rate = 44100
    # Пересэмплируем на фиксированную частоту, иначе размер окна в сэмплах давал бы
    # разную длительность окна для файлов с разной исходной частотой дискретизации.
    result = run(
        "ffmpeg",
        ["-hide_banner", "-i", path, "-af",
         f"aresample={sample_rate},asetnsamples=n={int(sample_rate * WINDOW_SECONDS)},"
         "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
         "-f", "null", "-"],
        timeout=TIMEOUT,
    )
    if not result.ok:
        message = [line for line in result.stderr.splitlines() if line.strip()]
        raise _FfmpegFailed(message[-1] if message else "ffmpeg завершился с ошибкой")
    windows = parse_metadata_series(result.stdout, "lavfi.astats.Overall.RMS_level")
    return _find_peaks(windows)


def _find_peaks(windows: list[tuple[float, float]]) -> list[dict]:
    if len(windows) < 3:
        return []
    levels = sorted(level for _, level in windows)
    median = levels[len(levels) // 2]
    deviations = sorted(abs(level - median) for level in levels)
    mad = deviations[len(deviations) // 2]
    threshold = max(PEAK_MIN_LIFT_DB, PEAK_MAD_FACTOR * mad)

    raw_peaks = []
    for i in range(1, len(windows) - 1):
        time, level = windows[i]
        prev_level = windows[i - 1][1]
        next_level = windows[i + 1][1]
        if level >= prev_level and level >= next_level and level - median >= threshold:
            raw_peaks.append({"time": round(time, 2), "level_db": round(level, 1)})

    # Соседние окна одного всплеска схлопываем в один пик — иначе агент увидит серию
    # почти одинаковых точек вместо одного акцента.
    merged: list[dict] = []
    for peak in raw_peaks:
        if merged and peak["time"] - merged[-1]["time"] <= WINDOW_SECONDS * 1.5:
            if peak["level_db"] > merged[-1]["level_db"]:
                merged[-1] = peak
        else:
            merged.append(peak)
    return merged
