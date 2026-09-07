"""Сборка команды ffmpeg из таймлайна (ТЗ п. 4.4)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.ffmpeg.compile import (
    PREVIEW_PRESET, CompileError, atempo_chain, build_command,
)
from backend.app.ffmpeg.probe import probe
from backend.app.ffmpeg.render import render
from backend.app.models import AnalysisStatus, Base, Folder, MediaFile, MediaType
from backend.app.timeline import ops
from backend.app.timeline.schema import empty_timeline

pytestmark = pytest.mark.skipif(settings.binary("ffmpeg") is None, reason="ffmpeg не установлен")


def ffmpeg(*args: str) -> None:
    subprocess.run([settings.binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture
def media(tmp_path: Path):
    """Настоящие маленькие файлы: команда потом реально исполняется."""
    clip = tmp_path / "clip.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=6",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
           "-pix_fmt", "yuv420p", "-shortest", str(clip))
    silent = tmp_path / "silent.mp4"
    ffmpeg("-f", "lavfi", "-i", "smptebars=size=640x360:rate=30:duration=4", "-pix_fmt", "yuv420p", str(silent))
    photo = tmp_path / "photo.jpg"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=800x600:duration=1", "-frames:v", "1", str(photo))
    music = tmp_path / "music.mp3"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=220:duration=20", str(music))

    engine = create_engine(f"sqlite:///{tmp_path / 'c.db'}", future=True)
    Base.metadata.create_all(engine)
    db = Session(engine)
    folder = Folder(path=str(tmp_path), name="s", is_root=True)
    db.add(folder)
    db.flush()

    def add(path: Path, kind: MediaType, duration: float | None, audio: bool) -> MediaFile:
        row = MediaFile(
            folder_id=folder.id, filename=path.name, path=str(path), type=kind,
            size=path.stat().st_size, modified=path.stat().st_mtime, duration=duration,
            has_audio=audio, analysis_status=AnalysisStatus.pending,
        )
        db.add(row)
        db.flush()
        return row

    files = {
        "clip": add(clip, MediaType.video, 6.0, True),
        "silent": add(silent, MediaType.video, 4.0, False),
        "photo": add(photo, MediaType.image, None, False),
        "music": add(music, MediaType.audio, 20.0, True),
    }
    yield db, files
    db.close()


def test_atempo_chain_covers_full_speed_range() -> None:
    for speed in (0.25, 0.5, 1.0, 1.5, 2.0, 4.0):
        product = 1.0
        for step in atempo_chain(speed):
            product *= float(step.split("=")[1])
        assert product == pytest.approx(speed, rel=1e-4)
    assert atempo_chain(1.0) == []


def test_empty_timeline_is_rejected(media) -> None:
    db, _ = media
    with pytest.raises(CompileError):
        build_command(db, empty_timeline(), Path("/tmp/out.mp4"))


def test_missing_source_is_reported(media, tmp_path: Path) -> None:
    db, files = media
    tl = empty_timeline()
    ops.add_clip(tl, source_id=files["clip"].id, kind="video", name="clip.mp4", source_duration=6.0)
    Path(files["clip"].path).unlink()

    with pytest.raises(CompileError, match="недоступны"):
        build_command(db, tl, tmp_path / "out.mp4")


def test_rendered_duration_matches_timeline(media, tmp_path: Path) -> None:
    db, files = media
    tl = empty_timeline()
    ops.add_clip(tl, source_id=files["clip"].id, kind="video", name="clip.mp4",
                 source_duration=6.0, out_point=3.0)
    ops.add_clip(tl, source_id=files["photo"].id, kind="image", name="photo.jpg")

    out = tmp_path / "out.mp4"
    render(build_command(db, tl, out, PREVIEW_PRESET))

    info = probe(out)
    assert info.duration == pytest.approx(6.0, abs=0.15)     # 3 с видео + 3 с фото
    assert info.resolution == "640x360"


def test_gap_between_clips_is_filled_with_black(media, tmp_path: Path) -> None:
    """Иначе второй клип начался бы раньше и разошёлся бы с музыкой."""
    db, files = media
    tl = empty_timeline()
    ops.add_clip(tl, source_id=files["clip"].id, kind="video", name="clip.mp4",
                 source_duration=6.0, out_point=2.0)
    ops.add_clip(tl, source_id=files["photo"].id, kind="image", name="photo.jpg", start=5.0)

    out = tmp_path / "gap.mp4"
    render(build_command(db, tl, out, PREVIEW_PRESET))

    assert probe(out).duration == pytest.approx(8.0, abs=0.2)   # 2 + 3 пустоты + 3


def test_speed_shortens_result(media, tmp_path: Path) -> None:
    db, files = media
    tl = empty_timeline()
    clip = ops.add_clip(tl, source_id=files["clip"].id, kind="video", name="clip.mp4",
                        source_duration=6.0, out_point=6.0)
    ops.set_speed(tl, clip.id, speed=2.0)

    out = tmp_path / "fast.mp4"
    render(build_command(db, tl, out, PREVIEW_PRESET))

    assert probe(out).duration == pytest.approx(3.0, abs=0.2)


def test_music_is_mixed_over_video(media, tmp_path: Path) -> None:
    db, files = media
    tl = empty_timeline()
    ops.add_clip(tl, source_id=files["clip"].id, kind="video", name="clip.mp4",
                 source_duration=6.0, out_point=4.0)
    ops.add_clip(tl, source_id=files["music"].id, kind="audio", name="music.mp3",
                 source_duration=20.0, out_point=4.0)

    out = tmp_path / "mixed.mp4"
    command = build_command(db, tl, out, PREVIEW_PRESET)
    render(command)

    assert command.has_audio is True
    assert probe(out).has_audio is True


def test_video_without_audio_track_produces_silent_file(media, tmp_path: Path) -> None:
    db, files = media
    tl = empty_timeline()
    ops.add_clip(tl, source_id=files["silent"].id, kind="video", name="silent.mp4",
                 source_duration=4.0)

    out = tmp_path / "silent_out.mp4"
    command = build_command(db, tl, out, PREVIEW_PRESET)
    render(command)

    assert command.has_audio is False
    assert probe(out).has_audio is False


def test_sources_with_different_size_and_fps_are_normalised(media, tmp_path: Path) -> None:
    """320x240@25 и 640x360@30 в одном монтаже — concat склеит их только после приведения."""
    db, files = media
    tl = empty_timeline()
    ops.add_clip(tl, source_id=files["clip"].id, kind="video", name="clip.mp4",
                 source_duration=6.0, out_point=2.0)
    ops.add_clip(tl, source_id=files["silent"].id, kind="video", name="silent.mp4",
                 source_duration=4.0, out_point=2.0)

    out = tmp_path / "mixed_sources.mp4"
    render(build_command(db, tl, out, PREVIEW_PRESET))

    info = probe(out)
    assert info.resolution == "640x360"
    assert info.duration == pytest.approx(4.0, abs=0.2)


def test_muted_video_clip_is_excluded_from_mix(media, tmp_path: Path) -> None:
    """Свой звук клипа можно выключить, чтобы он не наслаивался на музыку."""
    db, files = media
    tl = empty_timeline()
    clip = ops.add_clip(tl, source_id=files["clip"].id, kind="video", name="clip.mp4",
                        source_duration=6.0, out_point=3.0)
    ops.set_muted(tl, clip.id, muted=True)

    command = build_command(db, tl, tmp_path / "muted.mp4", PREVIEW_PRESET)

    assert command.has_audio is False


def test_muted_clip_still_leaves_music_audible(media, tmp_path: Path) -> None:
    db, files = media
    tl = empty_timeline()
    clip = ops.add_clip(tl, source_id=files["clip"].id, kind="video", name="clip.mp4",
                        source_duration=6.0, out_point=3.0)
    ops.set_muted(tl, clip.id, muted=True)
    ops.add_clip(tl, source_id=files["music"].id, kind="audio", name="music.mp3",
                 source_duration=20.0, out_point=3.0)

    out = tmp_path / "music_only.mp4"
    command = build_command(db, tl, out, PREVIEW_PRESET)
    render(command)

    assert command.has_audio is True
    assert probe(out).has_audio is True
def test_fade_adds_video_and_audio_fade_filters(media, tmp_path: Path) -> None:
    db, files = media
    tl = empty_timeline()
    clip = ops.add_clip(tl, source_id=files["clip"].id, kind="video", name="clip.mp4",
                        source_duration=6.0, out_point=4.0)
    ops.set_fade(tl, clip.id, fade_in=0.5, fade_out=1.0)

    command = build_command(db, tl, tmp_path / "faded.mp4", PREVIEW_PRESET)
    filter_complex = command.args[command.args.index("-filter_complex") + 1]

    assert "fade=t=in:st=0:d=0.5000" in filter_complex
    assert "fade=t=out:st=3.0000:d=1.0000" in filter_complex
    assert "afade=t=in:st=0:d=0.5000" in filter_complex
    assert "afade=t=out:st=3.0000:d=1.0000" in filter_complex


def test_fade_renders_without_error(media, tmp_path: Path) -> None:
    db, files = media
    tl = empty_timeline()
    clip = ops.add_clip(tl, source_id=files["clip"].id, kind="video", name="clip.mp4",
                        source_duration=6.0, out_point=4.0)
    ops.set_fade(tl, clip.id, fade_in=0.5, fade_out=1.0)

    out = tmp_path / "faded.mp4"
    render(build_command(db, tl, out, PREVIEW_PRESET))

    assert out.exists()


def test_transition_uses_xfade_in_filter_complex(media, tmp_path: Path) -> None:
    db, files = media
    tl = empty_timeline()
    a = ops.add_clip(tl, source_id=files["clip"].id, kind="video", name="a",
                     source_duration=6.0, out_point=3.0)
    b = ops.add_clip(tl, source_id=files["clip"].id, kind="video", name="b",
                     source_duration=6.0, in_point=3.0, out_point=6.0)
    ops.set_transition(tl, b.id, kind="crossfade", duration=0.5)

    command = build_command(db, tl, tmp_path / "xfade.mp4", PREVIEW_PRESET)
    filter_complex = command.args[command.args.index("-filter_complex") + 1]

    assert "xfade=transition=fade:duration=0.5000:offset=2.5000" in filter_complex


def test_transition_shortens_rendered_output(media, tmp_path: Path) -> None:
    db, files = media
    tl = empty_timeline()
    a = ops.add_clip(tl, source_id=files["clip"].id, kind="video", name="a",
                     source_duration=6.0, out_point=3.0)
    b = ops.add_clip(tl, source_id=files["clip"].id, kind="video", name="b",
                     source_duration=6.0, in_point=3.0, out_point=6.0)
    ops.set_transition(tl, b.id, kind="crossfade", duration=0.5)

    out = tmp_path / "xfade.mp4"
    render(build_command(db, tl, out, PREVIEW_PRESET))

    # 3 + 3 - 0.5 = 5.5 с (плюс небольшой запас на энкодинг/контейнер).
    assert abs(probe(out).duration - 5.5) < 0.2


def test_dip_to_black_transition_renders(media, tmp_path: Path) -> None:
    db, files = media
    tl = empty_timeline()
    a = ops.add_clip(tl, source_id=files["clip"].id, kind="video", name="a",
                     source_duration=6.0, out_point=3.0)
    b = ops.add_clip(tl, source_id=files["clip"].id, kind="video", name="b",
                     source_duration=6.0, in_point=3.0, out_point=6.0)
    ops.set_transition(tl, b.id, kind="dip_to_black", duration=0.4)

    command = build_command(db, tl, tmp_path / "dip.mp4", PREVIEW_PRESET)
    filter_complex = command.args[command.args.index("-filter_complex") + 1]
    assert "xfade=transition=fadeblack:duration=0.4000" in filter_complex

    render(command)
    assert (tmp_path / "dip.mp4").exists()


def test_text_clip_renders_without_error(media, tmp_path: Path) -> None:
    db, files = media
    tl = empty_timeline()
    ops.add_clip(tl, source_id=files["silent"].id, kind="video", name="silent.mp4",
                source_duration=4.0, out_point=4.0)
    ops.add_text_clip(tl, text="Привет, Вьетнам", duration=2.0, start=1.0)

    out = tmp_path / "with_text.mp4"
    render(build_command(db, tl, out, PREVIEW_PRESET))

    assert out.exists()
    assert abs(probe(out).duration - 4.0) < 0.2


def test_timeline_without_text_clips_is_unaffected(media, tmp_path: Path) -> None:
    """Без текстовых клипов путь рендера не меняется вообще — проверяем, что overlay не всплывает."""
    db, files = media
    tl = empty_timeline()
    ops.add_clip(tl, source_id=files["silent"].id, kind="video", name="silent.mp4",
                source_duration=4.0, out_point=4.0)

    command = build_command(db, tl, tmp_path / "no_text.mp4", PREVIEW_PRESET)
    filter_complex = command.args[command.args.index("-filter_complex") + 1]
    assert "overlay" not in filter_complex


def test_mixed_concat_and_transition_chain_renders(media, tmp_path: Path) -> None:
    """Регрессия: concat, за которым следует xfade дальше по цепочке, падал с
    несовпадением timebase («input link timebase do not match») — воспроизводится
    только на 3+ клипах, где склейка и переход чередуются (2 клипа этого не ловят)."""
    db, files = media
    tl = empty_timeline()
    a = ops.add_clip(tl, source_id=files["clip"].id, kind="video", name="a",
                     source_duration=6.0, out_point=2.0)
    b = ops.add_clip(tl, source_id=files["clip"].id, kind="video", name="b",
                     source_duration=6.0, in_point=2.0, out_point=4.0)
    c = ops.add_clip(tl, source_id=files["clip"].id, kind="video", name="c",
                     source_duration=6.0, in_point=4.0, out_point=6.0)
    # b встык к a без перехода (concat), c к b — с переходом (xfade после concat в цепочке).
    ops.set_transition(tl, c.id, kind="crossfade", duration=0.3)

    out = tmp_path / "mixed_chain.mp4"
    render(build_command(db, tl, out, PREVIEW_PRESET))

    assert out.exists()
    assert abs(probe(out).duration - 5.7) < 0.2   # 2+2+2-0.3 (один переход укорачивает на 0.3)
