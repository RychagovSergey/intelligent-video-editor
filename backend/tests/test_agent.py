"""Инструменты агента и цикл выполнения (ТЗ пп. 3.5, 4.5). Модель подменяется заглушкой."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.agent import runner
from backend.app.agent.search import searchable_text
from backend.app.agent.tools import Context, execute
from backend.app.config import settings
from backend.app.models import (
    AnalysisStatus, Base, Folder, MediaFile, MediaType, MetaCache, Project,
)
from backend.app.timeline import store
from backend.app.timeline.schema import empty_timeline


@pytest.fixture
def ctx(tmp_path: Path) -> Context:
    engine = create_engine(f"sqlite:///{tmp_path / 'a.db'}", future=True)
    Base.metadata.create_all(engine)
    db = Session(engine)
    folder = Folder(path=str(tmp_path), name="s", is_root=True)
    db.add(folder)
    db.add(Project(id=1, name="Тест", timeline_json=empty_timeline().model_dump_json()))
    db.flush()

    video = MediaFile(folder_id=folder.id, filename="street.mp4", path=str(tmp_path / "street.mp4"),
                      type=MediaType.video, size=1, modified=1.0, duration=12.0, has_audio=True,
                      analysis_status=AnalysisStatus.analyzed)
    music = MediaFile(folder_id=folder.id, filename="track.mp3", path=str(tmp_path / "track.mp3"),
                      type=MediaType.audio, size=1, modified=1.0, duration=180.0,
                      analysis_status=AnalysisStatus.analyzed)
    broken = MediaFile(folder_id=folder.id, filename="broken.mp4", path=str(tmp_path / "broken.mp4"),
                       type=MediaType.video, size=1, modified=1.0,
                       analysis_status=AnalysisStatus.corrupted)
    db.add_all([video, music, broken])
    db.flush()
    db.add(MetaCache(file_id=video.id, meta_json=json.dumps(
        {"description": "человек идёт по улице", "objects": ["человек", "улица"]}, ensure_ascii=False)))
    db.flush()

    yield Context(db=db, project_id=1, timeline=empty_timeline())
    db.close()


def test_add_clip_tool_puts_clip_on_timeline(ctx: Context) -> None:
    result = execute(ctx, "add_clip", {"source_id": 1, "duration": 4.0})

    assert "error" not in result
    assert result["clip"]["name"] == "street.mp4"
    assert result["clip"]["duration"] == 4.0
    assert ctx.changed is True


def test_add_clip_reports_unknown_source_to_model(ctx: Context) -> None:
    result = execute(ctx, "add_clip", {"source_id": 999})
    assert "нет в хранилище" in result["error"]


def test_corrupted_file_is_refused(ctx: Context) -> None:
    result = execute(ctx, "add_clip", {"source_id": 3})
    assert "повреждён" in result["error"]


def test_audio_goes_to_audio_track(ctx: Context) -> None:
    execute(ctx, "add_clip", {"source_id": 2, "duration": 30})
    tracks = execute(ctx, "get_timeline", {})["tracks"]

    assert tracks[0]["clips"] == []
    assert tracks[1]["clips"][0]["name"] == "track.mp3"


def test_muted_flag_is_applied_on_add(ctx: Context) -> None:
    result = execute(ctx, "add_clip", {"source_id": 1, "duration": 3, "muted": True})
    assert result["clip"]["muted"] is True


def test_fade_tool_sets_clip_fade(ctx: Context) -> None:
    execute(ctx, "add_clip", {"source_id": 1, "duration": 5})
    clip_id = execute(ctx, "get_timeline", {})["tracks"][0]["clips"][0]["id"]

    result = execute(ctx, "set_fade", {"clip_id": clip_id, "fade_in": 1.0, "fade_out": 0.5})

    assert result["clip"]["fade_in"] == 1.0
    assert result["clip"]["fade_out"] == 0.5
    assert ctx.changed is True


def test_fade_can_be_set_on_add(ctx: Context) -> None:
    result = execute(ctx, "add_clip", {"source_id": 1, "duration": 5, "fade_in": 0.5})
    assert result["clip"]["fade_in"] == 0.5


def test_transition_tool_links_adjacent_clips(ctx: Context) -> None:
    execute(ctx, "add_clip", {"source_id": 1, "duration": 5})
    execute(ctx, "add_clip", {"source_id": 1, "duration": 5})
    clips = execute(ctx, "get_timeline", {})["tracks"][0]["clips"]

    result = execute(ctx, "set_transition", {"clip_id": clips[1]["id"], "kind": "crossfade", "duration": 1.0})

    assert result["clip"]["transition_in"] == {"kind": "crossfade", "duration": 1.0}
    assert result["clip"]["start"] == 4.0
    assert ctx.changed is True


def test_transition_tool_reports_non_adjacent_error(ctx: Context) -> None:
    execute(ctx, "add_clip", {"source_id": 1, "duration": 5})
    execute(ctx, "add_clip", {"source_id": 1, "duration": 5, "start": 20})

    clips = execute(ctx, "get_timeline", {})["tracks"][0]["clips"]
    result = execute(ctx, "set_transition", {"clip_id": clips[1]["id"], "kind": "crossfade", "duration": 1.0})

    assert "встык" in result["error"]


def test_clear_transition_tool_removes_it(ctx: Context) -> None:
    execute(ctx, "add_clip", {"source_id": 1, "duration": 5})
    execute(ctx, "add_clip", {"source_id": 1, "duration": 5})
    clips = execute(ctx, "get_timeline", {})["tracks"][0]["clips"]
    execute(ctx, "set_transition", {"clip_id": clips[1]["id"], "kind": "crossfade", "duration": 1.0})

    result = execute(ctx, "clear_transition", {"clip_id": clips[1]["id"]})

    assert result["clip"]["transition_in"] is None
    assert result["clip"]["start"] == 5.0


def test_add_text_clip_tool(ctx: Context) -> None:
    result = execute(ctx, "add_text_clip", {"text": "Вьетнам", "duration": 2.0, "position": "top"})

    assert "error" not in result
    assert result["clip"]["kind"] == "text"
    assert result["clip"]["text"] == "Вьетнам"
    assert result["clip"]["position"] == "top"
    assert ctx.changed is True

    tracks = execute(ctx, "get_timeline", {})["tracks"]
    text_track = next(t for t in tracks if t["id"] == "text_1")
    assert len(text_track["clips"]) == 1


def test_add_text_clip_tool_rejects_empty_text(ctx: Context) -> None:
    result = execute(ctx, "add_text_clip", {"text": "   ", "duration": 2.0})
    assert "error" in result


@pytest.mark.skipif(settings.binary("ffmpeg") is None, reason="ffmpeg не установлен")
def test_get_audio_events_finds_silence(ctx: Context, tmp_path: Path) -> None:
    music_path = tmp_path / "track.mp3"
    ffmpeg = settings.binary("ffmpeg")
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:duration=2",
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[out]", "-map", "[out]", str(music_path)],
        check=True,
    )
    ctx.db.get(MediaFile, 2).path = str(music_path)
    ctx.db.flush()

    result = execute(ctx, "get_audio_events", {"source_id": 2})

    assert "error" not in result
    assert any(s["start"] < 1.5 < s["end"] for s in result["silences"])


def test_get_audio_events_rejects_non_audio(ctx: Context) -> None:
    result = execute(ctx, "get_audio_events", {"source_id": 1})
    assert "не аудио" in result["error"]


def test_get_audio_bpm_returns_grid(ctx: Context, tmp_path: Path) -> None:
    pytest.importorskip("librosa")
    from test_bpm import make_click_track

    music_path = tmp_path / "click.wav"
    make_click_track(music_path, bpm=120.0)
    ctx.db.get(MediaFile, 2).path = str(music_path)
    ctx.db.flush()

    result = execute(ctx, "get_audio_bpm", {"source_id": 2})

    assert "error" not in result
    assert 100 <= result["bpm"] <= 140
    assert result["beat_times"]
    assert result["downbeats"] == result["beat_times"][::4]


def test_get_audio_bpm_rejects_non_audio(ctx: Context) -> None:
    result = execute(ctx, "get_audio_bpm", {"source_id": 1})
    assert "не аудио" in result["error"]


def test_clear_timeline_removes_everything(ctx: Context) -> None:
    execute(ctx, "add_clip", {"source_id": 1, "duration": 3})
    execute(ctx, "clear_timeline", {})
    assert execute(ctx, "get_timeline", {})["tracks"][0]["clips"] == []


def test_unknown_tool_is_reported(ctx: Context) -> None:
    assert "Неизвестный инструмент" in execute(ctx, "render_movie", {})["error"]


def test_broken_arguments_do_not_crash(ctx: Context) -> None:
    assert "error" in execute(ctx, "add_clip", "{не json}")
    assert "error" in execute(ctx, "set_speed", {"clip_id": "нет", "speed": 2})


def test_search_falls_back_to_keywords_without_embeddings(ctx: Context, monkeypatch) -> None:
    from backend.app.agent import search as search_module

    def no_embeddings(_texts):
        raise search_module.EmbeddingsUnavailable("ключа нет")

    monkeypatch.setattr(search_module, "embed", no_embeddings)

    result = execute(ctx, "search_media", {"query": "человек улица"})

    assert result["mode"] == "keyword"
    assert result["results"][0]["filename"] == "street.mp4"


def test_search_media_excludes_clips_already_on_timeline(ctx: Context, monkeypatch) -> None:
    """Регрессия: без этого агент на большой библиотеке задирал limit (20→50→97...),
    просто чтобы докопаться до чего-то, ещё не занятого на таймлайне."""
    from backend.app.agent import search as search_module

    def no_embeddings(_texts):
        raise search_module.EmbeddingsUnavailable("ключа нет")

    monkeypatch.setattr(search_module, "embed", no_embeddings)

    execute(ctx, "add_clip", {"source_id": 1, "duration": 3})   # street.mp4 уже на таймлайне

    result = execute(ctx, "search_media", {"query": "человек улица"})

    assert "error" not in result
    assert all(r["filename"] != "street.mp4" for r in result["results"])


def test_list_media_excludes_clips_already_on_timeline(ctx: Context) -> None:
    execute(ctx, "add_clip", {"source_id": 1, "duration": 3})

    result = execute(ctx, "list_media", {"type": "video"})

    assert all(f["filename"] != "street.mp4" for f in result["files"])


def test_searchable_text_collects_meaningful_fields() -> None:
    meta = json.dumps({
        "description": "закат над морем",
        "objects": ["море", "солнце"],
        "scenes": [{"description": "волны"}],
        "duration": 12,
    }, ensure_ascii=False)

    text = searchable_text(meta, "sunset.mp4")

    assert "закат над морем" in text and "волны" in text and "море" in text
    assert "12" not in text          # технические поля в поиск не идут


def test_instructions_come_from_editor_agent_md() -> None:
    text = runner.instructions()
    assert "инструмент" in text.lower()
    assert runner.INSTRUCTIONS_FILE.name == "EDITOR_AGENT.md"


def test_agent_loop_executes_tools_then_stops(ctx: Context, monkeypatch) -> None:
    """Сценарий: модель зовёт add_clip, затем отвечает текстом — цикл должен завершиться."""
    scripted = [
        {"tool_calls": [{"id": "1", "function": {"name": "add_clip",
                                                 "arguments": json.dumps({"source_id": 1, "duration": 5})}}]},
        {"content": "Собрал 5 секунд из street.mp4"},
    ]
    calls: list[list[dict]] = []

    def fake_chat(messages, tools=None, **kwargs):
        calls.append(messages)
        return scripted.pop(0)

    monkeypatch.setattr(runner, "chat", fake_chat)
    monkeypatch.setattr(runner, "current_provider", lambda: _FakeProvider())
    monkeypatch.setattr(runner, "session_scope", _fake_scope(ctx))

    job = runner.AgentJob()
    job._run(1, "собери короткий ролик")

    assert job.state == "done"
    assert job.answer == "Собрал 5 секунд из street.mp4"
    # Агент работает со своей копией таймлайна и сохраняет её в проект.
    saved = store.load(ctx.db, 1)
    assert len(saved.tracks[0].clips) == 1
    assert saved.tracks[0].clips[0].duration == 5.0
    # Модель получила системный промпт из EDITOR_AGENT.md и результат инструмента.
    assert calls[0][0]["role"] == "system"
    assert any(m["role"] == "tool" for m in calls[1])


def test_agent_reports_tool_errors_back_to_model(ctx: Context, monkeypatch) -> None:
    scripted = [
        {"tool_calls": [{"id": "1", "function": {"name": "add_clip",
                                                 "arguments": json.dumps({"source_id": 999})}}]},
        {"content": "Такого файла нет"},
    ]
    seen: list[dict] = []

    def fake_chat(messages, tools=None, **kwargs):
        seen.extend(m for m in messages if m["role"] == "tool")
        return scripted.pop(0)

    monkeypatch.setattr(runner, "chat", fake_chat)
    monkeypatch.setattr(runner, "current_provider", lambda: _FakeProvider())
    monkeypatch.setattr(runner, "session_scope", _fake_scope(ctx))

    job = runner.AgentJob()
    job._run(1, "добавь файл 999")

    assert job.state == "done"
    assert any("нет в хранилище" in m["content"] for m in seen)
    assert any(entry["kind"] == "error" for entry in job.log)


class _FakeProvider:
    name = "test"
    model = "test-model"
    base_url = "http://localhost"
    api_key = "x"
    disable_thinking = False


def _fake_scope(ctx: Context):
    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield ctx.db

    return scope


def test_empty_response_without_changes_does_not_claim_success(ctx: Context, monkeypatch) -> None:
    """Регрессия: модель, которая не вызвала ни одного инструмента и ничего не написала,
    раньше получала фиктивный ответ «Монтаж собран.» — хотя таймлайн не менялся."""
    def empty_chat(messages, tools=None, **kwargs):
        return {"content": ""}

    monkeypatch.setattr(runner, "chat", empty_chat)
    monkeypatch.setattr(runner, "current_provider", lambda: _FakeProvider())
    monkeypatch.setattr(runner, "session_scope", _fake_scope(ctx))

    job = runner.AgentJob()
    job._run(1, "собери ролик")

    assert job.state == "done"
    assert job.answer != "Монтаж собран."
    assert "не внесла ни одного изменения" in job.answer
    saved = store.load(ctx.db, 1)
    assert saved.tracks[0].clips == []


def test_step_limit_without_changes_is_reported_honestly(ctx: Context, monkeypatch) -> None:
    """Если за весь бюджет шагов таймлайн не тронут, ответ не должен звучать как успех."""
    from backend.app.config import settings

    monkeypatch.setattr(settings, "max_steps_agent", 2)

    def read_only_chat(messages, tools=None, **kwargs):
        return {"tool_calls": [{"id": "1", "function": {"name": "get_timeline", "arguments": "{}"}}]}

    monkeypatch.setattr(runner, "chat", read_only_chat)
    monkeypatch.setattr(runner, "current_provider", lambda: _FakeProvider())
    monkeypatch.setattr(runner, "session_scope", _fake_scope(ctx))

    job = runner.AgentJob()
    job._run(1, "собери ролик")

    assert job.state == "done"
    assert "не успел ничего добавить" in job.answer


def test_stall_nudge_pushes_model_to_act(ctx: Context, monkeypatch) -> None:
    """Регрессия: модель реально зацикливалась на search_media, ни разу не дойдя до
    add_clip, даже когда EDITOR_AGENT.md прямо просил чередовать поиск и добавление.
    Нужна программная подсказка после N шагов без изменений — не только текст промпта."""
    from backend.app.config import settings
    monkeypatch.setattr(settings, "stall_nudge_threshold", 3)
    seen: list[list[dict]] = []

    def fake_chat(messages, tools=None, **kwargs):
        seen.append(messages)
        n = len(seen)
        if n <= 3:
            return {"tool_calls": [{"id": str(n), "function": {"name": "get_timeline", "arguments": "{}"}}]}
        if n == 4:
            return {"tool_calls": [{"id": "4", "function": {
                "name": "add_clip", "arguments": json.dumps({"source_id": 1, "duration": 3})}}]}
        return {"content": "Добавил клип"}

    monkeypatch.setattr(runner, "chat", fake_chat)
    monkeypatch.setattr(runner, "current_provider", lambda: _FakeProvider())
    monkeypatch.setattr(runner, "session_scope", _fake_scope(ctx))

    job = runner.AgentJob()
    job._run(1, "собери ролик")

    assert job.state == "done"
    assert job.answer == "Добавил клип"
    # К 4-му вызову модели подсказка уже должна быть в истории сообщений.
    assert any("Хватит искать" in m.get("content", "") for m in seen[3])
    assert any("подсказка модели" in entry["text"] for entry in job.log)
    saved = store.load(ctx.db, 1)
    assert len(saved.tracks[0].clips) == 1


def test_step_limit_comes_from_settings(ctx: Context, monkeypatch) -> None:
    """Предел шагов задаётся в .env: агент останавливается, а не ходит по кругу бесконечно."""
    from backend.app.config import settings

    monkeypatch.setattr(settings, "max_steps_agent", 3)

    calls = {"n": 0}

    def endless_chat(messages, tools=None, **kwargs):
        # Модель, которая всегда просит ещё один инструмент.
        calls["n"] += 1
        return {"tool_calls": [{"id": str(calls["n"]), "function": {
            "name": "get_timeline", "arguments": "{}"}}]}

    monkeypatch.setattr(runner, "chat", endless_chat)
    monkeypatch.setattr(runner, "current_provider", lambda: _FakeProvider())
    monkeypatch.setattr(runner, "session_scope", _fake_scope(ctx))

    job = runner.AgentJob()
    job._run(1, "зациклись")

    assert calls["n"] == 3
    assert job.state == "done"
    assert any("после 3 шагов" in entry["text"] for entry in job.log)
    assert job.snapshot()["max_steps"] == 3


# --- ритмическая разметка из анализа (кеш meta.json вместо разбора на каждый вызов) ---


def _mark_music_analyzed(ctx: Context, **rhythm) -> None:
    """Кладёт треку разметку, какую пишет анализ аудио."""
    ctx.db.add(MetaCache(file_id=2, meta_json=json.dumps({"type": "audio", **rhythm})))
    ctx.db.flush()


def test_get_audio_bpm_takes_grid_from_analysis(ctx: Context, monkeypatch) -> None:
    """Размеченный при анализе трек не разбирается заново на каждый вызов инструмента."""
    _mark_music_analyzed(ctx, bpm=99.4, beat_times=[1.0, 1.6, 2.2, 2.8, 3.4], downbeats=[1.0, 3.4])
    monkeypatch.setattr(
        "backend.app.agent.tools.detect_beat_grid",
        lambda *a, **k: pytest.fail("сетку посчитали заново, хотя она есть в meta.json"),
    )

    result = execute(ctx, "get_audio_bpm", {"source_id": 2})

    assert result["bpm"] == 99.4
    assert result["beat_times"] == [1.0, 1.6, 2.2, 2.8, 3.4]
    assert result["downbeats"] == [1.0, 3.4]


def test_get_audio_bpm_returns_grid_only_up_to_until(ctx: Context) -> None:
    """Сетка длинного трека целиком не влезает в ответ инструмента — отдаём нужное окно."""
    _mark_music_analyzed(ctx, bpm=100.0, beat_times=[0.6, 1.2, 1.8, 2.4, 3.0], downbeats=[0.6, 3.0])

    result = execute(ctx, "get_audio_bpm", {"source_id": 2, "until": 2.0})

    assert result["beat_times"] == [0.6, 1.2, 1.8]
    assert result["downbeats"] == [0.6]


def test_get_audio_events_takes_events_from_analysis(ctx: Context, monkeypatch) -> None:
    _mark_music_analyzed(ctx, silences=[{"start": 0.0, "end": 1.0}], peaks=[{"time": 4.0, "level_db": -8.0}])
    monkeypatch.setattr(
        "backend.app.agent.tools.detect_audio_events",
        lambda *a, **k: pytest.fail("события посчитали заново, хотя они есть в meta.json"),
    )

    result = execute(ctx, "get_audio_events", {"source_id": 2})

    assert result["peaks"] == [{"time": 4.0, "level_db": -8.0}]
    assert "note" not in result


def test_empty_peaks_are_explained_not_left_bare(ctx: Context) -> None:
    """Пустой peaks у ровного трека модель принимала за «ритма нет» и бросала монтаж под бит."""
    _mark_music_analyzed(ctx, silences=[], peaks=[])

    result = execute(ctx, "get_audio_events", {"source_id": 2})

    assert result["peaks"] == []
    assert "get_audio_bpm" in result["note"]


def test_search_media_filters_audio_by_tempo(ctx: Context, monkeypatch) -> None:
    """«Трек на 100–120 BPM» отбирается по разметке анализа, без разбора кандидатов."""
    from backend.app.agent import search as search_module

    monkeypatch.setattr(search_module, "embed",
                        lambda _texts: (_ for _ in ()).throw(search_module.EmbeddingsUnavailable("ключа нет")))
    folder_id = ctx.db.get(MediaFile, 2).folder_id
    fast = MediaFile(folder_id=folder_id, filename="fast.mp3", path="/tmp/fast.mp3",
                     type=MediaType.audio, size=1, modified=1.0, duration=100.0,
                     analysis_status=AnalysisStatus.analyzed)
    ctx.db.add(fast)
    ctx.db.flush()
    _mark_music_analyzed(ctx, bpm=110.0, title_hint="track")
    ctx.db.add(MetaCache(file_id=fast.id, meta_json=json.dumps({"type": "audio", "bpm": 145.0,
                                                               "title_hint": "track"})))
    ctx.db.flush()

    result = execute(ctx, "search_media", {"query": "track", "type": "audio",
                                           "min_bpm": 100, "max_bpm": 120})

    assert [r["filename"] for r in result["results"]] == ["track.mp3"]


def test_bpm_gets_into_searchable_text(ctx: Context) -> None:
    """У аудио нет описания — темп остаётся единственной приметой для поиска словами."""
    assert "110 BPM" in searchable_text(json.dumps({"type": "audio", "bpm": 110.0}), "track.mp3")
