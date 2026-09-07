"""Инструменты агента поверх операций таймлайна (ТЗ п. 4.5).

Тот же слой пригодится и как MCP-сервер: описания инструментов и исполнение
разделены, снаружи нужен только `TOOL_SPECS` и `execute`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ffmpeg.audio_events import detect_audio_events
from ..ffmpeg.bpm import detect_beat_grid
from ..models import AnalysisStatus, MediaFile, MediaType, MetaCache, enum_value
from ..timeline import ops, store
from ..timeline.schema import Timeline, empty_timeline
from . import search as media_search

log = logging.getLogger(__name__)

#: Ключи — строковые значения, потому что из базы тип приходит строкой.
KIND_BY_TYPE = {"video": "video", "image": "image", "audio": "audio"}

#: Сколько долей тактовой сетки максимум уходит модели за один вызов get_audio_bpm.
#: Ответ инструмента обрезается по длине уже после сериализации, поэтому длинную сетку
#: нужно ограничить здесь, пока обрезка не разорвала JSON посреди массива.
GRID_MAX_BEATS = 240


class ToolError(RuntimeError):
    """Инструмент вызван с недопустимыми аргументами — сообщение уходит модели."""


@dataclass
class Context:
    db: Session
    project_id: int
    timeline: Timeline
    changed: bool = False

    def save(self) -> None:
        if self.changed:
            store.save(self.db, self.project_id, self.timeline)


def _file(ctx: Context, source_id: int) -> MediaFile:
    file = ctx.db.get(MediaFile, source_id)
    if file is None or file.missing:
        raise ToolError(f"Файла с id={source_id} нет в хранилище")
    if file.analysis_status == AnalysisStatus.corrupted:
        raise ToolError(f"Файл {file.filename} повреждён и не годится для монтажа")
    return file


def _media_view(file: MediaFile, **extra) -> dict:
    """Что агент знает о файле: пропорции, частота кадров, звук и дата съёмки.

    Без этого он не мог отличить вертикальное видео от горизонтального
    и выстроить материал по хронологии.
    """
    shot = file.captured_at or None
    return {
        "source_id": file.id,
        "filename": file.filename,
        "type": enum_value(file.type),
        "duration": round(file.duration, 2) if file.duration else None,
        "resolution": f"{file.width}x{file.height}" if file.width and file.height else None,
        "orientation": _orientation(file),
        "fps": round(file.fps, 2) if file.fps else None,
        "has_audio": bool(file.has_audio),
        "shot_at": shot.isoformat() if shot else None,
        "analyzed": file.analysis_status == AnalysisStatus.analyzed,
        **extra,
    }


def _orientation(file: MediaFile) -> str | None:
    if not file.width or not file.height:
        return None
    if file.width > file.height:
        return "horizontal"
    return "vertical" if file.width < file.height else "square"


def _clip_view(clip) -> dict:
    view = {
        "id": clip.id,
        "name": clip.name,
        "kind": clip.kind,
        "start": clip.start,
        "duration": round(clip.duration, 3),
        "in_point": clip.in_point,
        "out_point": clip.out_point,
        "speed": clip.speed,
        "muted": clip.muted,
        "fade_in": clip.fade_in,
        "fade_out": clip.fade_out,
        "transition_in": (
            {"kind": clip.transition_in.kind, "duration": clip.transition_in.duration}
            if clip.transition_in else None
        ),
    }
    if clip.kind == "text":
        view.update(text=clip.text, font_size=clip.font_size, position=clip.position, animation=clip.animation)
    return view


# --------------------------------------------------------------------------- инструменты


def _used_source_ids(ctx: Context) -> set[int]:
    """source_id файлов, уже стоящих на таймлайне — чтобы search_media/list_media не
    выдавали их заново и агенту не приходилось задирать limit в поисках нового."""
    return {
        clip.source_id
        for track in ctx.timeline.tracks
        for clip in track.clips
        if clip.source_id is not None
    }


def tool_search_media(ctx: Context, args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("Не задан запрос для поиска")
    media_type = None
    if args.get("type"):
        try:
            media_type = MediaType(args["type"])
        except ValueError as exc:
            raise ToolError(f"Неизвестный тип: {args['type']}") from exc

    hits, mode = media_search.search(
        ctx.db, query,
        media_type=media_type,
        min_duration=args.get("min_duration"),
        max_duration=args.get("max_duration"),
        min_bpm=args.get("min_bpm"),
        max_bpm=args.get("max_bpm"),
        limit=int(args.get("limit") or 10),
        exclude_ids=_used_source_ids(ctx),
    )
    return {
        "mode": mode,
        "results": [_media_view(h.file, score=h.score, summary=h.summary) for h in hits],
    }


def tool_list_media(ctx: Context, args: dict) -> dict:
    stmt = select(MediaFile).where(MediaFile.missing.is_(False))
    if args.get("type"):
        stmt = stmt.where(MediaFile.type == MediaType(args["type"]))
    used = _used_source_ids(ctx)
    if used:
        stmt = stmt.where(MediaFile.id.not_in(used))
    order = MediaFile.captured_at if args.get("order") == "date" else MediaFile.filename
    rows = ctx.db.execute(stmt.order_by(order).limit(int(args.get("limit") or 50))).scalars()
    return {"files": [_media_view(f) for f in rows]}


def _meta(ctx: Context, file: MediaFile) -> dict:
    """Разобранный meta.json файла из кеша; пустой словарь, если анализа ещё не было."""
    cache = ctx.db.execute(
        select(MetaCache).where(MetaCache.file_id == file.id)
    ).scalar_one_or_none()
    if cache is None:
        return {}
    try:
        meta = json.loads(cache.meta_json)
    except json.JSONDecodeError:
        return {}
    return meta if isinstance(meta, dict) else {}


def tool_get_media_details(ctx: Context, args: dict) -> dict:
    """Полные метаданные одного файла, включая сцены с таймкодами."""
    file = _file(ctx, int(args["source_id"]))
    meta = _meta(ctx, file)

    view = _media_view(file)
    if meta:
        # Сцены с временами позволяют резать фрагмент по содержанию, а не наугад.
        view["scenes"] = meta.get("scenes") or []
        view["summary"] = meta.get("summary") or meta.get("description") or ""
        view["objects"] = meta.get("objects") or []
        view["style"] = meta.get("style") or ""
        view["emotions"] = meta.get("emotions") or ""
        if meta.get("bpm"):
            view["bpm"] = meta["bpm"]
    return view


def tool_get_timeline(ctx: Context, _args: dict) -> dict:
    return {
        "duration": round(ctx.timeline.duration, 3),
        "tracks": [
            {
                "id": t.id, "kind": t.kind, "name": t.name,
                "clips": [_clip_view(c) for c in t.sorted_clips()],
            }
            for t in ctx.timeline.tracks
        ],
    }


def tool_clear_timeline(ctx: Context, _args: dict) -> dict:
    ctx.timeline.tracks = empty_timeline().tracks
    ctx.changed = True
    return {"cleared": True}


def tool_add_clip(ctx: Context, args: dict) -> dict:
    file = _file(ctx, int(args["source_id"]))
    kind = KIND_BY_TYPE[enum_value(file.type)]
    in_point = float(args.get("in_point") or 0.0)
    out_point = args.get("out_point")
    if out_point is None and args.get("duration") is not None:
        out_point = in_point + float(args["duration"])

    try:
        clip = ops.add_clip(
            ctx.timeline,
            source_id=file.id, kind=kind, name=file.filename,
            track_id=args.get("track_id"),
            start=args.get("start"),
            in_point=in_point,
            out_point=float(out_point) if out_point is not None else None,
            source_duration=file.duration,
            speed=float(args.get("speed") or 1.0),
        )
        if args.get("muted"):
            ops.set_muted(ctx.timeline, clip.id, muted=True)
        if args.get("fade_in") or args.get("fade_out"):
            ops.set_fade(
                ctx.timeline, clip.id,
                fade_in=float(args["fade_in"]) if args.get("fade_in") is not None else None,
                fade_out=float(args["fade_out"]) if args.get("fade_out") is not None else None,
            )
    except (ops.TimelineError, ValueError) as exc:
        raise ToolError(str(exc)) from exc

    ctx.changed = True
    return {"clip": _clip_view(clip), "timeline_duration": round(ctx.timeline.duration, 3)}


def _mutate(ctx: Context, action) -> dict:
    try:
        clip = action()
    except (ops.TimelineError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    ctx.changed = True
    return {"clip": _clip_view(clip), "timeline_duration": round(ctx.timeline.duration, 3)}


def tool_trim_clip(ctx: Context, args: dict) -> dict:
    _, clip = ctx.timeline.find_clip(str(args["clip_id"]))
    if clip is None:
        raise ToolError(f"Клип {args['clip_id']} не найден")
    file = ctx.db.get(MediaFile, clip.source_id)
    return _mutate(ctx, lambda: ops.trim_clip(
        ctx.timeline, str(args["clip_id"]),
        in_point=args.get("in_point"), out_point=args.get("out_point"),
        source_duration=file.duration if file else None,
    ))


def tool_split_clip(ctx: Context, args: dict) -> dict:
    try:
        head, tail = ops.split_clip(ctx.timeline, str(args["clip_id"]), at=float(args["at"]))
    except (ops.TimelineError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    ctx.changed = True
    return {"head": _clip_view(head), "tail": _clip_view(tail)}


def tool_move_clip(ctx: Context, args: dict) -> dict:
    return _mutate(ctx, lambda: ops.move_clip(
        ctx.timeline, str(args["clip_id"]), start=float(args["start"]), track_id=args.get("track_id")
    ))


def tool_set_speed(ctx: Context, args: dict) -> dict:
    return _mutate(ctx, lambda: ops.set_speed(
        ctx.timeline, str(args["clip_id"]), speed=float(args["speed"])
    ))


def tool_mute_clip(ctx: Context, args: dict) -> dict:
    return _mutate(ctx, lambda: ops.set_muted(
        ctx.timeline, str(args["clip_id"]), muted=bool(args.get("muted", True))
    ))


def tool_set_fade(ctx: Context, args: dict) -> dict:
    return _mutate(ctx, lambda: ops.set_fade(
        ctx.timeline, str(args["clip_id"]),
        fade_in=float(args["fade_in"]) if args.get("fade_in") is not None else None,
        fade_out=float(args["fade_out"]) if args.get("fade_out") is not None else None,
    ))


def tool_delete_clip(ctx: Context, args: dict) -> dict:
    return _mutate(ctx, lambda: ops.delete_clip(ctx.timeline, str(args["clip_id"])))


def tool_set_transition(ctx: Context, args: dict) -> dict:
    return _mutate(ctx, lambda: ops.set_transition(
        ctx.timeline, str(args["clip_id"]),
        kind=str(args.get("kind") or "crossfade"),
        duration=float(args.get("duration") or 0.5),
    ))


def tool_clear_transition(ctx: Context, args: dict) -> dict:
    return _mutate(ctx, lambda: ops.clear_transition(ctx.timeline, str(args["clip_id"])))


def tool_add_text_clip(ctx: Context, args: dict) -> dict:
    try:
        clip = ops.add_text_clip(
            ctx.timeline,
            text=str(args["text"]),
            duration=float(args["duration"]),
            start=args.get("start"),
            font_size=int(args.get("font_size") or 48),
            position=str(args.get("position") or "bottom"),
            animation=str(args.get("animation") or "fade"),
        )
    except (ops.TimelineError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    ctx.changed = True
    return {"clip": _clip_view(clip), "timeline_duration": round(ctx.timeline.duration, 3)}


def tool_set_text_properties(ctx: Context, args: dict) -> dict:
    return _mutate(ctx, lambda: ops.set_text_properties(
        ctx.timeline, str(args["clip_id"]),
        text=args.get("text"),
        font_size=int(args["font_size"]) if args.get("font_size") is not None else None,
        position=args.get("position"),
        animation=args.get("animation"),
    ))


def tool_get_audio_events(ctx: Context, args: dict) -> dict:
    """Паузы и резкие всплески громкости аудиофайла — чтобы монтировать видео под музыку."""
    file = _file(ctx, int(args["source_id"]))
    if enum_value(file.type) != "audio":
        raise ToolError(f"Файл {file.filename} не аудио — get_audio_events только для аудиофайлов")

    # Разметка считается при анализе файла и лежит в meta.json; разбор на лету остаётся
    # только для файлов, которые ещё не анализировали (или анализировали старой версией).
    meta = _meta(ctx, file)
    if "silences" in meta or "peaks" in meta:
        silences, peaks = meta.get("silences") or [], meta.get("peaks") or []
    else:
        events = detect_audio_events(file.path, duration=file.duration)
        if not events.ok:
            raise ToolError(f"Не удалось проанализировать {file.filename}: {events.error}")
        silences, peaks = events.silences, events.peaks

    result = {
        "source_id": file.id,
        "duration": round(file.duration, 2) if file.duration else None,
        "silences": silences,
        "peaks": peaks,
    }
    if not peaks:
        # Пустой peaks модель принимала за «ритма нет» и переставала резать под музыку,
        # хотя у ровного по громкости трека акцентов и не может быть — ритм у него есть,
        # просто искать его надо тактовой сеткой.
        result["note"] = (
            "Всплесков громкости нет — трек ровный по динамике, это нормально. "
            "Для ритма возьми тактовую сетку: get_audio_bpm."
        )
    return result


def tool_get_audio_bpm(ctx: Context, args: dict) -> dict:
    """Темп и тактовая сетка аудиофайла — чтобы резать/менять клипы точно на долях."""
    file = _file(ctx, int(args["source_id"]))
    if enum_value(file.type) != "audio":
        raise ToolError(f"Файл {file.filename} не аудио — get_audio_bpm только для аудиофайлов")

    meta = _meta(ctx, file)
    if meta.get("beat_times"):
        bpm = meta.get("bpm")
        all_beats, all_downbeats = meta["beat_times"], meta.get("downbeats") or []
    else:
        grid = detect_beat_grid(file.path)
        if not grid.ok:
            raise ToolError(f"Не удалось определить темп {file.filename}: {grid.error}")
        bpm, all_beats, all_downbeats = grid.bpm, grid.beat_times, grid.downbeats

    # Ответ инструмента обрезается по длине перед отправкой модели, а сетка шестиминутного
    # трека — почти тысяча долей: без окна массив обрывался посреди числа, и модель
    # получала битый JSON вместо таймингов. Ролику всё равно нужна сетка только на свою
    # длину, поэтому отдаём её до `until` и не длиннее GRID_MAX_BEATS долей.
    beats = all_beats
    until = args.get("until")
    if until is not None:
        beats = [t for t in beats if t <= float(until)]
    trimmed = len(beats) > GRID_MAX_BEATS
    if trimmed:
        beats = beats[:GRID_MAX_BEATS]
    result = {
        "source_id": file.id,
        "bpm": bpm,
        "beat_times": beats,
        "downbeats": [t for t in all_downbeats if not beats or t <= beats[-1]],
    }
    if trimmed:
        result["note"] = (
            f"Сетка отдана только до {beats[-1]} с ({GRID_MAX_BEATS} долей). "
            "Нужен участок дальше — вызови ещё раз с параметром until."
        )
    return result


def tool_close_gaps(ctx: Context, args: dict) -> dict:
    track_id = str(args.get("track_id") or "video_1")
    try:
        track = ops.close_gaps(ctx.timeline, track_id)
    except ops.TimelineError as exc:
        raise ToolError(str(exc)) from exc
    ctx.changed = True
    return {"track": track.id, "duration": round(ctx.timeline.duration, 3)}


HANDLERS = {
    "search_media": tool_search_media,
    "list_media": tool_list_media,
    "get_media_details": tool_get_media_details,
    "get_timeline": tool_get_timeline,
    "clear_timeline": tool_clear_timeline,
    "add_clip": tool_add_clip,
    "trim_clip": tool_trim_clip,
    "split_clip": tool_split_clip,
    "move_clip": tool_move_clip,
    "set_speed": tool_set_speed,
    "set_fade": tool_set_fade,
    "mute_clip": tool_mute_clip,
    "get_audio_events": tool_get_audio_events,
    "get_audio_bpm": tool_get_audio_bpm,
    "set_transition": tool_set_transition,
    "clear_transition": tool_clear_transition,
    "add_text_clip": tool_add_text_clip,
    "set_text_properties": tool_set_text_properties,
    "delete_clip": tool_delete_clip,
    "close_gaps": tool_close_gaps,
}


def _spec(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        },
    }


TOOL_SPECS = [
    _spec("search_media",
          "Найти медиафайлы по смыслу описания (люди, природа, настроение). Файлы, уже "
          "стоящие на таймлайне, в выдаче не появляются — поднимать limit, чтобы "
          "докопаться до них, не нужно.", {
        "query": {"type": "string", "description": "что ищем, своими словами"},
        "type": {"type": "string", "enum": ["video", "image", "audio"]},
        "min_duration": {"type": "number", "description": "минимальная длительность, с"},
        "max_duration": {"type": "number", "description": "максимальная длительность, с"},
        "min_bpm": {"type": "number",
                    "description": "минимальный темп трека; только для type: audio — темп известен "
                                   "из анализа, отдельно считать его для отбора не нужно"},
        "max_bpm": {"type": "number", "description": "максимальный темп трека"},
        "limit": {"type": "integer", "description": "сколько вернуть, по умолчанию 10"},
    }, ["query"]),
    _spec("list_media",
          "Перечислить файлы хранилища без поиска. Файлы, уже стоящие на таймлайне, в "
          "списке не появляются.", {
        "type": {"type": "string", "enum": ["video", "image", "audio"]},
        "order": {"type": "string", "enum": ["name", "date"],
                  "description": "date — по дате съёмки, для хронологии"},
        "limit": {"type": "integer"},
    }),
    _spec("get_media_details",
          "Полные метаданные файла: описание, объекты и сцены с таймкодами — "
          "по ним можно выбрать конкретный фрагмент внутри видео.", {
              "source_id": {"type": "integer"},
          }, ["source_id"]),
    _spec("get_timeline", "Показать текущий монтаж: дорожки и клипы с их id.", {}),
    _spec("clear_timeline", "Убрать с таймлайна все клипы и начать сборку заново.", {}),
    _spec("add_clip", "Добавить клип в конец дорожки или в заданную позицию.", {
        "source_id": {"type": "integer", "description": "id файла из хранилища"},
        "start": {"type": "number", "description": "позиция на таймлайне, с; без неё — в конец"},
        "in_point": {"type": "number", "description": "с какой секунды исходника брать"},
        "out_point": {"type": "number", "description": "по какую секунду исходника брать"},
        "duration": {"type": "number",
                     "description": "длительность фрагмента ИСХОДНИКА, с (вместо out_point); при speed != 1 "
                                    "на таймлайне клип займёт duration / speed — чтобы занять слот L на "
                                    "скорости S, передавай duration = L * S"},
        "speed": {"type": "number", "description": "0.25–4.0, по умолчанию 1"},
        "muted": {"type": "boolean", "description": "выключить собственный звук клипа"},
        "fade_in": {"type": "number", "description": "плавное появление, с"},
        "fade_out": {"type": "number", "description": "плавное затухание, с"},
        "track_id": {"type": "string", "description": "video_1 или audio_1"},
    }, ["source_id"]),
    _spec("trim_clip", "Изменить точки входа и выхода клипа.", {
        "clip_id": {"type": "string"},
        "in_point": {"type": "number"},
        "out_point": {"type": "number"},
    }, ["clip_id"]),
    _spec("split_clip", "Разрезать клип в указанной позиции таймлайна.", {
        "clip_id": {"type": "string"},
        "at": {"type": "number", "description": "позиция разреза на таймлайне, с"},
    }, ["clip_id", "at"]),
    _spec("move_clip", "Переместить клип по времени или на другую дорожку того же типа.", {
        "clip_id": {"type": "string"},
        "start": {"type": "number"},
        "track_id": {"type": "string"},
    }, ["clip_id", "start"]),
    _spec("set_speed", "Задать скорость клипа (0.25–4.0).", {
        "clip_id": {"type": "string"},
        "speed": {"type": "number"},
    }, ["clip_id", "speed"]),
    _spec("set_fade", "Задать плавное появление/затухание клипа (видео и звук вместе).", {
        "clip_id": {"type": "string"},
        "fade_in": {"type": "number", "description": "плавное появление, с"},
        "fade_out": {"type": "number", "description": "плавное затухание, с"},
    }, ["clip_id"]),
    _spec("mute_clip", "Выключить или включить собственный звук клипа.", {
        "clip_id": {"type": "string"},
        "muted": {"type": "boolean"},
    }, ["clip_id"]),
    _spec("get_audio_events",
          "Паузы (silences) и всплески громкости (peaks) аудиофайла, в секундах. Полезны на "
          "речи и записях с событиями; у ровного по громкости музыкального трека peaks "
          "законно пуст — это не ошибка и не повод бросать ритм, ритм ищи через get_audio_bpm.", {
              "source_id": {"type": "integer"},
          }, ["source_id"]),
    _spec("get_audio_bpm",
          "Темп (bpm) и тактовая сетка аудиофайла: beat_times — секунды каждой доли, "
          "downbeats — приближённые начала тактов (каждая 4-я доля). Основной инструмент "
          "монтажа под музыку: start клипа бери прямо из этих значений, а длительность — "
          "как разность соседних границ.", {
              "source_id": {"type": "integer"},
              "until": {"type": "number",
                        "description": "до какой секунды нужна сетка; для ролика на 60 с — until: 60"},
          }, ["source_id"]),
    _spec("set_transition",
          "Переход (кроссфейд, dip to black) между этим клипом и предыдущим на видеодорожке. "
          "Клипы должны стоять встык. Этот клип и ВСЕ последующие сдвигаются влево на duration: "
          "дыры не будет, но ролик станет короче на duration, а каждая склейка после перехода "
          "уедет с доли музыки. При монтаже под бит либо не ставь переходы, либо заранее "
          "удлини слот на duration.", {
              "clip_id": {"type": "string"},
              "kind": {"type": "string", "enum": ["crossfade", "dip_to_black"], "description": "по умолчанию crossfade"},
              "duration": {"type": "number", "description": "секунды, короче любого из соседних клипов"},
          }, ["clip_id"]),
    _spec("clear_transition", "Убрать переход с клипа.", {"clip_id": {"type": "string"}}, ["clip_id"]),
    _spec("add_text_clip",
          "Добавить текстовый слой (заголовок/подпись) поверх видео на дорожку text_1.", {
              "text": {"type": "string", "description": "до 200 символов"},
              "duration": {"type": "number", "description": "сколько секунд показывать"},
              "start": {"type": "number", "description": "позиция на таймлайне, с; без неё — в конец дорожки"},
              "font_size": {"type": "integer", "description": "по умолчанию 48"},
              "position": {"type": "string", "enum": ["top", "center", "bottom"], "description": "по умолчанию bottom"},
              "animation": {"type": "string", "enum": ["none", "fade"], "description": "по умолчанию fade"},
          }, ["text", "duration"]),
    _spec("set_text_properties", "Изменить текст/оформление уже добавленного текстового клипа.", {
        "clip_id": {"type": "string"},
        "text": {"type": "string"},
        "font_size": {"type": "integer"},
        "position": {"type": "string", "enum": ["top", "center", "bottom"]},
        "animation": {"type": "string", "enum": ["none", "fade"]},
    }, ["clip_id"]),
    _spec("delete_clip", "Удалить клип с таймлайна.", {"clip_id": {"type": "string"}}, ["clip_id"]),
    _spec("close_gaps", "Сдвинуть клипы дорожки встык, убрав пустоты.", {
        "track_id": {"type": "string", "description": "по умолчанию video_1"},
    }),
]


def execute(ctx: Context, name: str, raw_args: str | dict) -> dict:
    """Выполняет инструмент. Ошибку возвращает как результат — модель должна её увидеть."""
    handler = HANDLERS.get(name)
    if handler is None:
        return {"error": f"Неизвестный инструмент: {name}"}

    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError as exc:
            return {"error": f"Аргументы не разобрались как JSON: {exc}"}
    else:
        args = raw_args or {}

    try:
        return handler(ctx, args)
    except ToolError as exc:
        return {"error": str(exc)}
    except (KeyError, TypeError, ValueError) as exc:
        return {"error": f"Некорректные аргументы: {exc}"}
