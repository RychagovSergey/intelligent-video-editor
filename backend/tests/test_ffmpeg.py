"""Тесты слоя ffmpeg на реальных файлах, которые генерирует сам ffmpeg."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.app.config import settings
from backend.app.ffmpeg.frames import extract_frames
from backend.app.ffmpeg.probe import probe
from backend.app.ffmpeg.thumbs import ensure_thumbnail
from backend.app.models import MediaType

pytestmark = pytest.mark.skipif(settings.binary("ffmpeg") is None, reason="ffmpeg не установлен")


def ffmpeg(*args: str) -> None:
    subprocess.run(
        [settings.binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
    )


@pytest.fixture
def video(tmp_path: Path) -> Path:
    out = tmp_path / "clip.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=6",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
           "-pix_fmt", "yuv420p", "-shortest", str(out))
    return out


@pytest.fixture
def image(tmp_path: Path) -> Path:
    out = tmp_path / "frame.png"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=800x600:duration=1", "-frames:v", "1", str(out))
    return out


@pytest.fixture
def audio_with_cover(tmp_path: Path, image: Path) -> Path:
    out = tmp_path / "track.mp3"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-i", str(image),
           "-map", "0:a", "-map", "1:v", "-disposition:v:0", "attached_pic", str(out))
    return out


def test_probe_reads_video_specs(video: Path) -> None:
    info = probe(video)
    assert info.ok
    assert info.resolution == "640x360"
    assert info.fps == 25.0
    assert info.duration == pytest.approx(6.0, abs=0.2)
    assert info.video_codec == "h264"
    assert info.has_audio is True


def test_probe_image_has_no_fps_or_duration(image: Path) -> None:
    info = probe(image)
    assert info.ok
    assert info.resolution == "800x600"
    assert info.fps is None
    assert info.duration is None
    assert info.has_audio is False


def test_cover_art_is_not_treated_as_video(audio_with_cover: Path) -> None:
    """Обложка mp3 приходит как attached_pic и раньше давала «видео 90000 fps»."""
    info = probe(audio_with_cover)
    assert info.ok
    assert info.has_audio is True
    assert info.has_cover_art is True
    assert info.video_codec is None
    assert info.width is None
    assert info.fps is None


def test_corrupted_file_reports_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00\x01\x02 not a real video")
    info = probe(broken)
    assert not info.ok
    assert info.error


def test_frames_are_extracted_and_temp_dir_removed(video: Path) -> None:
    with extract_frames(video, every_seconds=2.0, max_frames=12) as frames:
        assert len(frames) == 3          # 6 секунд с шагом 2 с
        assert all(f.path.exists() for f in frames)
        assert [f.time for f in frames] == [0.0, 2.0, 4.0]
        tmp_dir = frames[0].path.parent
    assert not tmp_dir.exists()


def test_frame_count_is_capped(video: Path) -> None:
    """Слишком частая выборка не должна заваливать модель сотней кадров."""
    with extract_frames(video, every_seconds=0.1, max_frames=5) as frames:
        assert len(frames) <= 5


def test_nth_frame_mode_works(video: Path) -> None:
    with extract_frames(video, every_nth_frame=25, max_frames=12) as frames:
        assert 5 <= len(frames) <= 7     # 150 кадров / каждый 25-й


def test_thumbnail_is_created_and_cached(video: Path) -> None:
    st = video.stat()
    first = ensure_thumbnail(video, MediaType.video, st.st_size, st.st_mtime)
    assert first is not None and first.exists()
    mtime = first.stat().st_mtime

    second = ensure_thumbnail(video, MediaType.video, st.st_size, st.st_mtime)
    assert second == first
    assert second.stat().st_mtime == mtime   # повторно не перерисовывается


def test_thumbnail_for_audio_without_cover_is_none(tmp_path: Path) -> None:
    out = tmp_path / "silence.mp3"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(out))
    st = out.stat()
    assert ensure_thumbnail(out, MediaType.audio, st.st_size, st.st_mtime) is None


def test_probe_marks_audio_without_cover(tmp_path: Path) -> None:
    """Флаг обложки отличает треки с картинкой от треков без неё."""
    plain = tmp_path / "plain.mp3"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(plain))

    info = probe(plain)

    assert info.has_audio is True
    assert info.has_cover_art is False
