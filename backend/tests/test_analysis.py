"""Тесты анализа: модель подменяется заглушкой, ffmpeg работает настоящий."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from backend.app.analysis import analyzer
from backend.app.analysis.analyzer import analyze_file, write_sidecar
from backend.app.analysis.ollama_client import ChatResult, OllamaError
from backend.app.analysis.schemas import ImageMeta, InvalidModelOutput, parse_json
from backend.app.config import settings
from backend.app.media_types import meta_path_for
from backend.app.models import MediaType

pytestmark = pytest.mark.skipif(settings.binary("ffmpeg") is None, reason="ffmpeg не установлен")


class FakeClient:
    """Заглушка Ollama: отдаёт заготовленные ответы и считает вызовы."""

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, int]] = []

    def chat(self, model, prompt, images=None, *, max_tokens=200, temperature=0.1) -> ChatResult:
        self.calls.append((prompt[:24], len(images or [])))
        content = self.responses.pop(0) if self.responses else '{"description":"кадр"}'
        return ChatResult(content=content)


class BrokenClient:
    def chat(self, *a, **kw) -> ChatResult:
        raise OllamaError("Ollama недоступна на http://localhost:11434")


def ffmpeg(*args: str) -> None:
    subprocess.run([settings.binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture
def photo(tmp_path: Path) -> Path:
    out = tmp_path / "photo.jpg"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=1920x1080:duration=1", "-frames:v", "1", str(out))
    return out


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    out = tmp_path / "clip.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=6", "-pix_fmt", "yuv420p", str(out))
    return out


IMAGE_ANSWER = json.dumps({
    "description": "человек идёт по улице",
    "objects": ["человек", "улица"],
    "style": "городской",
    "colors": ["серый", "синий"],
    "emotions": "спокойствие",
    "quality": "высокое",
}, ensure_ascii=False)


def test_image_analysis_fills_meta(photo: Path) -> None:
    client = FakeClient(IMAGE_ANSWER)
    meta = analyze_file(photo, MediaType.image, model="qwen2.5vl:7b", client=client)

    assert meta["type"] == "image"
    assert meta["description"] == "человек идёт по улице"
    assert meta["objects"] == ["человек", "улица"]
    assert meta["resolution"] == "1920x1080"     # разрешение оригинала, не уменьшенной копии
    assert meta["model"] == "qwen2.5vl:7b"
    assert "analysis_failed" not in meta
    assert len(client.calls) == 1


def test_image_is_downscaled_before_sending(photo: Path) -> None:
    """В модель уходит копия не больше MODEL_IMAGE_MAX_SIDE по большей стороне."""
    from backend.app.ffmpeg.probe import probe

    seen: list[tuple[Path, int, int]] = []

    class Recorder(FakeClient):
        def chat(self, model, prompt, images=None, **kw) -> ChatResult:
            # Копия живёт только на время запроса — измеряем её здесь же.
            for image in images or []:
                info = probe(image)
                seen.append((image, info.width or 0, info.height or 0))
            return ChatResult(content=IMAGE_ANSWER)

    analyze_file(photo, MediaType.image, model="m", client=Recorder())

    assert len(seen) == 1
    sent_path, width, height = seen[0]
    assert sent_path != photo
    assert max(width, height) == settings.model_image_max_side


def test_video_analysis_builds_scenes_and_summary(clip: Path) -> None:
    frame_answer = json.dumps({"description": "кадр", "objects": ["объект"]}, ensure_ascii=False)
    summary_answer = json.dumps({"summary": "общее описание", "objects": ["объект"]}, ensure_ascii=False)
    client = FakeClient(*([frame_answer] * 3), summary_answer)

    meta = analyze_file(clip, MediaType.video, model="m", client=client, every_seconds=2.0, max_frames=3)

    assert meta["type"] == "video"
    assert meta["resolution"] == "640x360"
    assert meta["fps"] == 25.0
    assert len(meta["scenes"]) == 3
    assert [s["time"] for s in meta["scenes"]] == [0.0, 2.0, 4.0]
    assert meta["summary"] == "общее описание"
    # Сводка запрашивается текстом, без картинок.
    assert client.calls[-1][1] == 0


def test_audio_needs_no_model(tmp_path: Path) -> None:
    track = tmp_path / "track.mp3"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(track))
    client = FakeClient()

    meta = analyze_file(track, MediaType.audio, model="m", client=client)

    assert meta["type"] == "audio"
    assert meta["duration"] == pytest.approx(2.0, abs=0.2)
    assert meta["model"] is None
    assert client.calls == []


def test_audio_analysis_writes_rhythm_markup(tmp_path: Path) -> None:
    """Темп, тактовая сетка и события громкости считаются один раз — при анализе трека."""
    pytest.importorskip("librosa")
    from test_bpm import make_click_track

    track = tmp_path / "metronome.wav"
    make_click_track(track, bpm=120.0)

    meta = analyze_file(track, MediaType.audio, model="m", client=FakeClient())

    assert 108 <= meta["bpm"] <= 132
    assert meta["beat_times"]
    assert meta["downbeats"] == meta["beat_times"][::4]
    assert "silences" in meta and "peaks" in meta


def test_audio_analysis_survives_rhythm_failure(tmp_path: Path, monkeypatch) -> None:
    """Не определился темп — у файла остаются технические поля, анализ не считается сорванным."""
    from backend.app.ffmpeg.bpm import BeatGrid

    monkeypatch.setattr(analyzer, "detect_beat_grid", lambda _p: BeatGrid(error="librosa не установлен"))
    track = tmp_path / "track.mp3"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(track))

    meta = analyze_file(track, MediaType.audio, model="m", client=FakeClient())

    assert meta["type"] == "audio"
    assert "analysis_failed" not in meta
    assert "bpm" not in meta


def test_model_unavailable_marks_analysis_failed(photo: Path) -> None:
    meta = analyze_file(photo, MediaType.image, model="m", client=BrokenClient())

    assert meta["analysis_failed"] is True
    assert "недоступна" in meta["error"]


def test_corrupted_file_is_not_sent_to_model(tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00 not a video")
    client = FakeClient(IMAGE_ANSWER)

    meta = analyze_file(broken, MediaType.video, model="m", client=client)

    assert meta["analysis_failed"] is True
    assert client.calls == []


def test_invalid_output_is_retried_once(photo: Path) -> None:
    client = FakeClient("это не json вовсе", IMAGE_ANSWER)

    meta = analyze_file(photo, MediaType.image, model="m", client=client)

    assert meta["description"] == "человек идёт по улице"
    assert len(client.calls) == 2


def test_persistently_invalid_output_fails_cleanly(photo: Path) -> None:
    client = FakeClient("мусор", "снова мусор")

    meta = analyze_file(photo, MediaType.image, model="m", client=client)

    assert meta["analysis_failed"] is True


def test_sidecar_name_and_atomic_write(photo: Path) -> None:
    target = write_sidecar(photo, {"description": "тест", "objects": []})

    assert target == meta_path_for(photo)
    assert target.name == "photo.jpg.meta.json"
    assert json.loads(target.read_text(encoding="utf-8"))["description"] == "тест"
    # Временные файлы записи не остаются в папке.
    assert not list(photo.parent.glob(".meta_*"))


def test_parse_json_tolerates_wrapping() -> None:
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Вот ответ: {"a": 2} готово') == {"a": 2}
    with pytest.raises(InvalidModelOutput):
        parse_json("совсем не json")


def test_meta_normalises_loose_model_output() -> None:
    meta = ImageMeta(**parse_json('{"description":["две","части"],"objects":"человек, машина","colors":null}'))
    assert meta.description == "две, части"
    assert meta.objects == ["человек", "машина"]
    assert meta.colors == []


def test_failure_path_works_when_type_comes_from_database(photo: Path) -> None:
    """Из базы тип приходит строкой — ветка ошибки не должна падать на .value."""
    meta = analyze_file(photo, "image", model="m", client=BrokenClient())

    assert meta["analysis_failed"] is True
    assert meta["type"] == "image"


def test_failure_path_works_for_corrupted_file_with_string_type(tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00 not a video")

    meta = analyze_file(broken, "video", model="m", client=FakeClient())

    assert meta["analysis_failed"] is True
    assert meta["type"] == "video"
