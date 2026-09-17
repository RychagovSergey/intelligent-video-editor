"""Сквозной сценарий: скан → анализ → монтаж инструментами агента → экспорт (этап 8).

Юниты проверяют каждую стадию по отдельности, а стыки между ними ловились только на
реальных проектах (см. `xfade` после `concat` в восьмой партии). Здесь одна сессия
проходит весь конвейер теми же входами, что и приложение: `scan_roots`,
`probe_pending`, `analyze_file` + сайдкар, который подхватывает повторный скан (Р-1),
слой инструментов `execute()`, `store`, `build_command` + `render`. Фоновые джобы и
HTTP-роутеры не задействованы — они лишь оборачивают эти же функции.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app.agent.tools import Context, execute
from backend.app.analysis.analyzer import analyze_file, write_sidecar
from backend.app.config import settings
from backend.app.ffmpeg.compile import build_command, export_preset
from backend.app.ffmpeg.probe import probe
from backend.app.ffmpeg.render import render
from backend.app.models import AnalysisStatus, MediaFile, MediaType, MetaCache, Project
from backend.app.probing import probe_pending
from backend.app.scanner import scan_roots
from backend.app.timeline import store
from backend.app.timeline.schema import empty_timeline
from test_analysis import FakeClient, ffmpeg

pytestmark = pytest.mark.skipif(settings.binary("ffmpeg") is None, reason="ffmpeg не установлен")


def _frame(description: str, quality: str) -> str:
    return json.dumps({"description": description, "objects": ["лодка"], "quality": quality}, ensure_ascii=False)


def test_scan_analyze_edit_export(db, tmp_path: Path) -> None:
    librosa = pytest.importorskip("librosa")
    from test_bpm import make_click_track

    # --- хранилище: настоящие файлы, как их создала бы камера и плеер ---
    root = tmp_path / "library"
    (root / "trip").mkdir(parents=True)
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=6",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
           "-pix_fmt", "yuv420p", "-shortest", str(root / "trip" / "boat.mp4"))
    ffmpeg("-f", "lavfi", "-i", "smptebars=size=320x240:rate=25:duration=5",
           "-pix_fmt", "yuv420p", str(root / "trip" / "street.mp4"))
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=400x300:duration=1", "-frames:v", "1", str(root / "photo.jpg"))
    make_click_track(root / "beat.wav", bpm=120.0, n_beats=40)   # 20 с ровного метронома
    (root / "notes.txt").write_text("не медиа")

    # --- 1. скан + характеристики ---
    stats = scan_roots(db, [root])
    assert stats.files_added == 4                   # txt не индексируется
    probe_pending(db)
    db.flush()
    by_name = {f.filename: f for f in db.execute(select(MediaFile)).scalars()}
    assert by_name["boat.mp4"].duration == pytest.approx(6.0, abs=0.2)
    assert by_name["boat.mp4"].has_audio is True
    assert by_name["street.mp4"].has_audio is False
    assert by_name["beat.wav"].type == MediaType.audio
    assert all(f.analysis_status == AnalysisStatus.pending for f in by_name.values())

    # --- 2. анализ: VL подменена, ритм считается по-настоящему; сайдкар — на диск ---
    answers = {
        "boat.mp4": FakeClient(_frame("лодка на реке", "высокое"), _frame("лодка", "высокое"),
                               _frame("тёмный кадр", "низкое"),
                               json.dumps({"summary": "лодка плывёт по реке", "objects": ["лодка", "река"]},
                                          ensure_ascii=False)),
        "street.mp4": FakeClient(_frame("улица", "низкое"), _frame("улица", "низкое"), _frame("улица", "среднее"),
                                 json.dumps({"summary": "тёмная улица"}, ensure_ascii=False)),
        "photo.jpg": FakeClient(json.dumps({"description": "закат", "quality": "высокое"}, ensure_ascii=False)),
        "beat.wav": FakeClient(),
    }
    for name, file in by_name.items():
        meta = analyze_file(Path(file.path), file.type, model="m", client=answers[name],
                            every_seconds=2.0, max_frames=3)
        assert not meta.get("analysis_failed"), meta.get("error")
        write_sidecar(Path(file.path), meta)

    # Повторный скан подхватывает сайдкары — так приложение узнаёт о результатах анализа.
    rescan = scan_roots(db, [root])
    assert rescan.meta_loaded == 4
    db.flush()
    cache = {c.file_id: json.loads(c.meta_json) for c in db.execute(select(MetaCache)).scalars()}
    boat, street, beat = by_name["boat.mp4"], by_name["street.mp4"], by_name["beat.wav"]
    assert boat.analysis_status == AnalysisStatus.analyzed
    assert cache[boat.id]["quality"] == "высокое"          # мода по сценам, одна тёмная не топит
    assert cache[street.id]["quality"] == "низкое"
    assert 108 <= cache[beat.id]["bpm"] <= 132              # метроном на 120
    assert cache[beat.id]["beat_times"]

    # --- 3. монтаж теми же инструментами, что и встроенный агент / MCP ---
    db.add(Project(id=1, name="e2e", timeline_json=empty_timeline().model_dump_json()))
    db.flush()
    ctx = Context(db=db, project_id=1, timeline=store.load(db, 1))

    found = execute(ctx, "search_media", {"query": "лодка", "type": "video"})
    assert "error" not in found
    assert found["results"][0]["source_id"] == boat.id
    assert "качество низкое" in next(r for r in found["results"] if r["source_id"] == street.id)["summary"]

    grid = execute(ctx, "get_audio_bpm", {"source_id": beat.id, "until": 10})
    beats = grid["beat_times"]
    assert beats and beats[-1] <= 10

    # Сетка под бит: смены на долях, как велит EDITOR_AGENT.md.
    first, second, end = beats[0], beats[4], beats[8]
    assert "error" not in execute(ctx, "add_clip", {
        "source_id": boat.id, "start": 0, "in_point": 0, "duration": first, "muted": True, "track_id": "video_1"})
    assert "error" not in execute(ctx, "add_clip", {
        "source_id": street.id, "start": first, "duration": second - first, "muted": True, "track_id": "video_1"})
    third = execute(ctx, "add_clip", {
        "source_id": boat.id, "start": second, "in_point": 2.0, "duration": end - second, "muted": True,
        "track_id": "video_1"})
    assert "error" not in third
    assert "error" not in execute(ctx, "add_clip", {
        "source_id": beat.id, "track_id": "audio_1", "start": 0, "in_point": 0, "out_point": end, "fade_out": 1.0})
    assert "error" not in execute(ctx, "add_text_clip", {"text": "e2e", "duration": 1.5, "start": 0.2})
    # Переход после обычной склейки — тот самый стык, который когда-то ронял рендер.
    assert "error" not in execute(ctx, "set_transition", {"clip_id": third["clip"]["id"], "duration": 0.3})

    timeline = execute(ctx, "get_timeline", {})
    video = next(t for t in timeline["tracks"] if t["id"] == "video_1")["clips"]
    video_end = max(c["start"] + c["duration"] for c in video)
    # Кроссфейд сдвинул видеоряд на свою длительность; музыка и текст остались на местах —
    # ролик в целом длиной с музыку, и именно это агенту велено сверять после переходов.
    assert video_end == pytest.approx(end - 0.3, abs=0.01)
    assert timeline["duration"] == pytest.approx(end, abs=0.01)

    # --- 4. сохранение и перечитывание проекта ---
    ctx.save()
    db.flush()
    reloaded = store.load(db, 1)
    assert reloaded.duration == pytest.approx(end, abs=0.01)
    assert sum(len(t.clips) for t in reloaded.tracks) == 5

    # --- 5. экспорт ---
    out = tmp_path / "final.mp4"
    command = build_command(db, reloaded, out, export_preset(container="mp4", quality="low", width=320, height=180))
    render(command)
    info = probe(out)
    assert info.ok, info.error
    assert info.duration == pytest.approx(end, abs=0.3)
    assert (info.width, info.height) == (320, 180)
    assert info.has_audio is True
    assert "loudnorm" in " ".join(command.args)
