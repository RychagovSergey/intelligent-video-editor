"""Огибающая амплитуды для отрисовки waveform на таймлайне."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.app.config import settings
from backend.app.ffmpeg.waveform import ensure_waveform, extract_waveform

pytestmark = pytest.mark.skipif(settings.binary("ffmpeg") is None, reason="ffmpeg не установлен")


def ffmpeg(*args: str) -> None:
    subprocess.run(
        [settings.binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
    )


@pytest.fixture
def loud_then_silent(tmp_path: Path) -> Path:
    """2 с громкого сигнала + 2 с цифровой тишины — форма огибающей известна заранее."""
    out = tmp_path / "track.mp3"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2",
           "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:duration=2",
           "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[out]", "-map", "[out]", str(out))
    return out


def test_waveform_has_requested_number_of_points(loud_then_silent: Path) -> None:
    values = extract_waveform(str(loud_then_silent), duration=4.0, points=40)
    assert len(values) == 40
    assert all(0.0 <= v <= 1.0 for v in values)


def test_waveform_is_silent_in_second_half(loud_then_silent: Path) -> None:
    values = extract_waveform(str(loud_then_silent), duration=4.0, points=40)
    first_half = values[:18]     # с запасом от границы 2.0 с
    second_half = values[22:]
    assert max(first_half) > 0.0
    assert max(second_half) == 0.0


def test_waveform_of_empty_duration_is_flat(tmp_path: Path) -> None:
    assert extract_waveform(str(tmp_path / "nope.mp3"), duration=0.0, points=10) == [0.0] * 10


def test_ensure_waveform_uses_cache(loud_then_silent: Path, monkeypatch) -> None:
    from backend.app.ffmpeg import waveform as waveform_module

    first = ensure_waveform(loud_then_silent, mtime=1.0, duration=4.0, points=20)

    def fail(*_a, **_k):
        raise AssertionError("должен был взять значение из кеша, а не считать заново")

    monkeypatch.setattr(waveform_module, "extract_waveform", fail)
    second = ensure_waveform(loud_then_silent, mtime=1.0, duration=4.0, points=20)

    assert second == first
