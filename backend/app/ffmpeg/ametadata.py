"""Разбор вывода фильтра ffmpeg `ametadata=print` — общий код для `audio_events.py`
(тишина/пики для агента) и `waveform.py` (огибающая для отрисовки на таймлайне)."""
from __future__ import annotations

import math
import re

_FRAME_RE = re.compile(r"pts_time:\s*(-?[\d.]+)")


def parse_metadata_series(stdout: str, key: str) -> list[tuple[float, float]]:
    """Пары (время окна, значение) для строки `{key}=...`. Тишина (-inf/nan) отбрасывается."""
    value_re = re.compile(rf"{re.escape(key)}=\s*(-?[\d.]+|-?inf|nan)")
    series: list[tuple[float, float]] = []
    pending_time: float | None = None
    for line in stdout.splitlines():
        frame_match = _FRAME_RE.search(line)
        if frame_match:
            pending_time = float(frame_match.group(1))
            continue
        value_match = value_re.search(line)
        if value_match and pending_time is not None:
            value = float(value_match.group(1))
            if math.isfinite(value):
                series.append((pending_time, value))
            pending_time = None
    return series
