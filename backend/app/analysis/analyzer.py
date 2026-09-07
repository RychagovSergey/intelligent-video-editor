"""Анализ медиафайлов моделью Qwen2.5-VL и запись meta.json (ТЗ пп. 3.2, 5)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..ffmpeg.audio_events import detect_audio_events
from ..ffmpeg.bpm import detect_beat_grid
from ..ffmpeg.frames import extract_frames
from ..ffmpeg.probe import TechInfo, probe
from ..ffmpeg.resize import downscaled
from ..paths import meta_path_for
from ..models import MediaType, enum_value
from .ollama_client import ChatResult, OllamaClient, OllamaError
from .prompts import FRAME_PROMPT, IMAGE_PROMPT, SUMMARY_PROMPT
from .schemas import ImageMeta, InvalidModelOutput, SceneMeta, SummaryMeta, parse_json

log = logging.getLogger(__name__)

#: Предел кадров на видео задаётся в .env (MAX_FRAMES): он же определяет фактический шаг.
DEFAULT_MAX_FRAMES = settings.max_frames
IMAGE_TOKENS = 200
FRAME_TOKENS = 160
SUMMARY_TOKENS = 200

#: Обратный вызов прогресса внутри одного файла: (шаг, всего, подпись).
StepCb = Callable[[int, int, str], None]


class AnalysisError(RuntimeError):
    pass


def _ask(client: OllamaClient, model: str, prompt: str, images: list[Path], tokens: int) -> dict:
    """Один запрос к модели с повтором, если ответ не разобрался."""
    last: Exception | None = None
    for attempt in (1, 2):
        result: ChatResult = client.chat(model, prompt, images, max_tokens=tokens)
        try:
            return parse_json(result.content)
        except InvalidModelOutput as exc:
            last = exc
            log.warning("Попытка %s: модель вернула неразбираемый ответ (%s)", attempt, exc)
            prompt = prompt + "\nВерни строго один объект JSON без пояснений."
    raise AnalysisError(str(last))


def analyze_image(client: OllamaClient, model: str, path: Path, tech: TechInfo) -> dict:
    # В модель уходит уменьшенная копия, в meta.json — разрешение оригинала.
    with downscaled(path) as small:
        data = _ask(client, model, IMAGE_PROMPT, [small], IMAGE_TOKENS)
    meta = ImageMeta(**data)
    return {"type": "image", "resolution": tech.resolution, **meta.model_dump()}


def analyze_video(
    client: OllamaClient,
    model: str,
    path: Path,
    tech: TechInfo,
    *,
    every_seconds: float | None = None,
    every_nth_frame: int | None = None,
    max_frames: int = DEFAULT_MAX_FRAMES,
    on_step: StepCb | None = None,
) -> dict:
    scenes: list[dict] = []

    with extract_frames(
        path,
        every_seconds=every_seconds or settings.frame_sample_seconds,
        every_nth_frame=every_nth_frame,
        max_frames=max_frames,
    ) as frames:
        if not frames:
            raise AnalysisError("Не удалось извлечь кадры для анализа")

        total = len(frames) + 1  # кадры + сводка
        for frame in frames:
            if on_step:
                on_step(frame.index, total, f"кадр {frame.index + 1}/{len(frames)}")
            data = _ask(client, model, FRAME_PROMPT, [frame.path], FRAME_TOKENS)
            scene = SceneMeta(time=frame.time, **{k: v for k, v in data.items() if k != "time"})
            scenes.append(scene.model_dump())

        if on_step:
            on_step(len(frames), total, "сводка")

    listing = "\n".join(f'{s["time"]:.1f} с: {s["description"]}' for s in scenes)
    summary_data = _ask(
        client, model, SUMMARY_PROMPT.format(scenes=listing), [], SUMMARY_TOKENS
    )
    summary = SummaryMeta(**summary_data)

    return {
        "type": "video",
        "duration": tech.duration,
        "resolution": tech.resolution,
        "fps": tech.fps,
        "has_audio": tech.has_audio,
        "scenes": scenes,
        "summary": summary.summary,
        "objects": summary.objects,
        "style": summary.style,
        "emotions": summary.emotions,
    }


def audio_rhythm(path: Path, duration: float | None = None) -> dict:
    """Темп с тактовой сеткой и события громкости трека — для монтажа под музыку.

    Считается один раз при анализе и живёт в meta.json рядом с техданными: иначе агент
    не может отобрать трек по темпу, не разобрав каждого кандидата по очереди, а разбор
    стоит секунды на файл. Неудача разметки не проваливает анализ — без librosa или на
    нечитаемом потоке у файла просто останутся одни технические поля.
    """
    meta: dict = {}

    grid = detect_beat_grid(str(path))
    if grid.ok and grid.bpm:
        meta.update(bpm=grid.bpm, beat_times=grid.beat_times, downbeats=grid.downbeats)
    else:
        log.info("Темп %s не определён: %s", path.name, grid.error or "нет выраженного бита")

    events = detect_audio_events(str(path), duration=duration)
    if events.ok:
        meta.update(silences=events.silences, peaks=events.peaks)
    else:
        log.info("События громкости %s не получены: %s", path.name, events.error)

    return meta


def analyze_audio(path: Path, tech: TechInfo) -> dict:
    """Для аудио модель не нужна: технические данные плюс ритмическая разметка (ТЗ п. 3.2)."""
    return {
        "type": "audio",
        "duration": tech.duration,
        "bitrate": tech.bitrate,
        "codec": tech.audio_codec,
        "has_cover_art": tech.has_cover_art,
        "title_hint": path.stem,
        **audio_rhythm(path, tech.duration),
    }


def write_sidecar(path: Path, meta: dict) -> Path:
    """Атомарная запись метаданных в кеш хранилища: временный файл, затем rename."""
    target = meta_path_for(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".meta_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return target


def analyze_file(
    path: Path,
    kind: MediaType,
    *,
    model: str,
    client: OllamaClient | None = None,
    every_seconds: float | None = None,
    every_nth_frame: int | None = None,
    max_frames: int = DEFAULT_MAX_FRAMES,
    on_step: StepCb | None = None,
) -> dict:
    """Анализирует файл и возвращает содержимое meta.json.

    Ошибки не поднимаются наверх: вместо этого в метаданные попадает
    `analysis_failed` с текстом причины (ТЗ п. 3.2).
    """
    client = client or OllamaClient()
    started = datetime.now(timezone.utc)
    tech = probe(path)

    base = {
        "model": model,
        "analyzed_at": started.isoformat(),
        "source": {"filename": path.name, "size": path.stat().st_size if path.exists() else None},
    }

    if not tech.ok:
        return {**base, "type": enum_value(kind), "analysis_failed": True, "error": tech.error}

    try:
        if kind == MediaType.image:
            meta = analyze_image(client, model, path, tech)
        elif kind == MediaType.video:
            meta = analyze_video(
                client, model, path, tech,
                every_seconds=every_seconds,
                every_nth_frame=every_nth_frame,
                max_frames=max_frames,
                on_step=on_step,
            )
        else:
            meta = analyze_audio(path, tech)
            base["model"] = None       # модель для аудио не вызывается
    except (OllamaError, AnalysisError) as exc:
        log.warning("Анализ не удался для %s: %s", path, exc)
        return {**base, "type": enum_value(kind), "analysis_failed": True, "error": str(exc)}

    return {**base, **meta}
