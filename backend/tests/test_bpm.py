"""Темп и тактовая сетка аудиофайла (get_audio_bpm)."""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from backend.app.ffmpeg.bpm import detect_beat_grid

librosa = pytest.importorskip("librosa")


def make_click_track(path: Path, *, bpm: float = 120.0, n_beats: int = 40, sr: int = 22050) -> None:
    """Ровный «метроном»: короткие затухающие щелчки на известном интервале."""
    interval = 60.0 / bpm
    click_seconds = 0.03
    n_samples = int(interval * n_beats * sr)
    samples = [0] * n_samples
    for beat in range(n_beats):
        start = int(beat * interval * sr)
        for i in range(int(click_seconds * sr)):
            idx = start + i
            if idx < n_samples:
                envelope = 1.0 - i / (click_seconds * sr)
                samples[idx] = int(32767 * envelope * math.sin(2 * math.pi * 1000 * i / sr))
    with wave.open(str(path), "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(struct.pack(f"<{n_samples}h", *samples))


@pytest.fixture
def click_track(tmp_path: Path) -> Path:
    out = tmp_path / "metronome.wav"
    make_click_track(out, bpm=120.0)
    return out


def test_bpm_is_close_to_true_tempo(click_track: Path) -> None:
    grid = detect_beat_grid(str(click_track))
    assert grid.ok
    assert 108 <= grid.bpm <= 132   # ровный метроном на 120 — допуск на неточность трекера


def test_beat_times_are_roughly_evenly_spaced(click_track: Path) -> None:
    grid = detect_beat_grid(str(click_track))
    diffs = [b - a for a, b in zip(grid.beat_times, grid.beat_times[1:])]
    assert diffs   # доли вообще нашлись
    assert all(0.35 <= d <= 0.65 for d in diffs)   # истинный интервал 0.5 с


def test_downbeats_are_every_fourth_beat(click_track: Path) -> None:
    grid = detect_beat_grid(str(click_track))
    assert grid.downbeats == grid.beat_times[::4]


def test_missing_file_reports_error(tmp_path: Path) -> None:
    grid = detect_beat_grid(str(tmp_path / "nope.mp3"))
    assert not grid.ok
    assert grid.error
