"""Экспорт: пресеты, проверки и реальный рендер (ТЗ пп. 3.6, 5)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.export import ExportError, check_space, safe_filename
from backend.app.ffmpeg.compile import build_command, export_preset
from backend.app.ffmpeg.probe import probe
from backend.app.ffmpeg.render import render
from backend.app.models import AnalysisStatus, Base, Folder, MediaFile, MediaType
from backend.app.timeline import ops
from backend.app.timeline.schema import empty_timeline

pytestmark = pytest.mark.skipif(settings.binary("ffmpeg") is None, reason="ffmpeg не установлен")


@pytest.fixture
def project(tmp_path: Path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [settings.binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=4",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-pix_fmt", "yuv420p", "-shortest", str(clip)],
        check=True,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'e.db'}", future=True)
    Base.metadata.create_all(engine)
    db = Session(engine)
    folder = Folder(path=str(tmp_path), name="s", is_root=True)
    db.add(folder)
    db.flush()
    row = MediaFile(
        folder_id=folder.id, filename=clip.name, path=str(clip), type=MediaType.video,
        size=clip.stat().st_size, modified=clip.stat().st_mtime, duration=4.0,
        has_audio=True, analysis_status=AnalysisStatus.pending,
    )
    db.add(row)
    db.flush()

    timeline = empty_timeline()
    timeline.width, timeline.height, timeline.fps = 640, 360, 25.0
    ops.add_clip(timeline, source_id=row.id, kind="video", name=clip.name, source_duration=4.0)

    yield db, timeline
    db.close()


def test_quality_maps_to_crf() -> None:
    assert "-crf" in export_preset(quality="high").video_args
    assert export_preset(quality="high").video_args[-1] == "18"
    assert export_preset(quality="medium").video_args[-1] == "23"
    assert export_preset(quality="low").video_args[-1] == "28"


def test_explicit_bitrate_replaces_crf() -> None:
    args = export_preset(container="mp4", bitrate="8M").video_args
    assert "-b:v" in args and "8M" in args
    assert "-crf" not in args


def test_containers_use_expected_codecs() -> None:
    assert "prores_ks" in export_preset(container="mov").video_args
    assert "pcm_s16le" in export_preset(container="mov").audio_args
    assert "libvpx-vp9" in export_preset(container="webm").video_args
    assert "libx264" in export_preset(container="mp4").video_args


def test_filename_is_sanitised() -> None:
    assert safe_filename("Мой/монтаж: финал?") == "Мой_монтаж_ финал_"
    assert safe_filename("   ") == "export"
    assert len(safe_filename("д" * 300)) == 120


def test_space_check_rejects_impossible_export(tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="Недостаточно места"):
        check_space(tmp_path, "mov", duration=500_000)


def test_space_check_passes_for_small_export(tmp_path: Path) -> None:
    check_space(tmp_path, "mp4", duration=10)


def test_mp4_export_keeps_project_resolution(project, tmp_path: Path) -> None:
    db, timeline = project
    out = tmp_path / "out.mp4"

    preset = export_preset(container="mp4", quality="medium",
                           width=timeline.width, height=timeline.height, fps=timeline.fps)
    render(build_command(db, timeline, out, preset))

    info = probe(out)
    assert info.resolution == "640x360"
    assert info.duration == pytest.approx(4.0, abs=0.2)
    assert info.video_codec == "h264"
    assert info.audio_codec == "aac"


def test_quality_affects_file_size(project, tmp_path: Path) -> None:
    db, timeline = project
    sizes = {}
    for quality in ("high", "low"):
        out = tmp_path / f"{quality}.mp4"
        preset = export_preset(container="mp4", quality=quality,
                               width=timeline.width, height=timeline.height, fps=timeline.fps)
        render(build_command(db, timeline, out, preset))
        sizes[quality] = out.stat().st_size

    assert sizes["high"] > sizes["low"]


def test_mov_prores_export(project, tmp_path: Path) -> None:
    db, timeline = project
    out = tmp_path / "out.mov"

    preset = export_preset(container="mov", width=timeline.width,
                           height=timeline.height, fps=timeline.fps)
    render(build_command(db, timeline, out, preset))

    info = probe(out)
    assert info.video_codec == "prores"
    assert info.duration == pytest.approx(4.0, abs=0.2)
