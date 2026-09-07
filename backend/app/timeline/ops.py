"""Операции над таймлайном (ТЗ п. 3.3).

Все функции меняют переданный `Timeline` на месте и возвращают затронутый клип.
Проверки общие для UI и агента: и то и другое ходит через этот слой.
"""
from __future__ import annotations

from .schema import (
    DEFAULT_IMAGE_DURATION,
    MAX_SPEED,
    MIN_CLIP_DURATION,
    MIN_SPEED,
    TEXT_POSITIONS,
    TRANSITION_KINDS,
    Clip,
    Timeline,
    Track,
    Transition,
)

#: Насколько неточно клипы всё ещё считаются «встык» (сравнение чисел с плавающей точкой).
ADJACENCY_TOLERANCE = 0.01


class TimelineError(ValueError):
    """Операция невозможна: клип не найден, выходит за границы исходника и т. п."""


def _require_track(timeline: Timeline, track_id: str | None, kind: str) -> Track:
    track = timeline.track(track_id) if track_id else timeline.first_track(kind)  # type: ignore[arg-type]
    if track is None:
        raise TimelineError(f"Дорожка не найдена: {track_id or kind}")
    return track


def _ensure_track(timeline: Timeline, track_id: str, kind: str, name: str) -> Track:
    """Как `_require_track`, но создаёт дорожку, если её нет — старые проекты (до
    появления текстовых слоёв) сохранены без дорожки `text_1`."""
    track = timeline.track(track_id) or timeline.first_track(kind)  # type: ignore[arg-type]
    if track is None:
        track = Track(id=track_id, kind=kind, name=name)  # type: ignore[arg-type]
        timeline.tracks.append(track)
    return track


def _require_clip(timeline: Timeline, clip_id: str) -> tuple[Track, Clip]:
    track, clip = timeline.find_clip(clip_id)
    if track is None or clip is None:
        raise TimelineError(f"Клип не найден: {clip_id}")
    return track, clip


def _overlaps(track: Track, clip: Clip, ignore_id: str | None = None) -> Clip | None:
    """Пересечение клипов — не считая намеренного перекрытия, которое даёт переход
    (`Clip.transition_in`) между соседними клипами."""
    for other in track.clips:
        if other.id == clip.id or other.id == ignore_id:
            continue
        if not (clip.start < other.end and other.start < clip.end):
            continue
        earlier, later = (other, clip) if other.start <= clip.start else (clip, other)
        allowed = later.transition_in.duration if later.transition_in else 0.0
        if earlier.end - later.start <= allowed + 1e-6:
            continue
        return other
    return None


def _append_position(track: Track) -> float:
    return track.duration


def add_clip(
    timeline: Timeline,
    *,
    source_id: int,
    kind: str,
    name: str = "",
    track_id: str | None = None,
    start: float | None = None,
    in_point: float = 0.0,
    out_point: float | None = None,
    source_duration: float | None = None,
    speed: float = 1.0,
) -> Clip:
    """Добавляет клип. Без `start` клип встаёт в конец дорожки."""
    track_kind = "audio" if kind == "audio" else "video"
    track = _require_track(timeline, track_id, track_kind)

    if out_point is None:
        if kind == "image":
            out_point = in_point + DEFAULT_IMAGE_DURATION
        elif source_duration:
            out_point = source_duration
        else:
            raise TimelineError("Неизвестна длительность исходника — задайте out_point")

    if source_duration and out_point > source_duration + 0.01:
        # Для изображения длительность показа задаём мы сами, ограничение не нужно.
        if kind != "image":
            out_point = source_duration

    clip = Clip(
        source_id=source_id,
        name=name,
        kind=kind,  # type: ignore[arg-type]
        start=_append_position(track) if start is None else start,
        in_point=in_point,
        out_point=out_point,
        speed=speed,
    )

    if (other := _overlaps(track, clip)) is not None:
        # Явно указанная позиция занята — ставим в конец, а не молча накладываем.
        clip.start = _append_position(track)
        if _overlaps(track, clip) is not None:
            raise TimelineError(f"Позиция занята клипом {other.name or other.id}")

    track.clips.append(clip)
    return clip


def move_clip(
    timeline: Timeline, clip_id: str, *, start: float, track_id: str | None = None
) -> Clip:
    track, clip = _require_clip(timeline, clip_id)
    if clip.transition_in is not None:
        # Перемещение клипа рвёт связь «встык» с предыдущим — переход сбрасываем, а не
        # оставляем висящим на новой, уже не примыкающей позиции.
        _untransition(track, clip)
    target = _require_track(timeline, track_id, track.kind) if track_id else track

    if target.kind != track.kind:
        raise TimelineError("Клип нельзя перенести на дорожку другого типа")

    previous_start, previous_track = clip.start, track
    clip.start = max(0.0, round(start, 4))

    if target is not track:
        track.clips.remove(clip)
        target.clips.append(clip)

    if _overlaps(target, clip) is not None:
        clip.start = previous_start
        if target is not previous_track:
            target.clips.remove(clip)
            previous_track.clips.append(clip)
        raise TimelineError("В этом месте дорожки уже есть клип")
    return clip


def trim_clip(
    timeline: Timeline,
    clip_id: str,
    *,
    in_point: float | None = None,
    out_point: float | None = None,
    source_duration: float | None = None,
    keep_start: bool = True,
) -> Clip:
    """Меняет точки входа/выхода. По умолчанию позиция начала клипа сохраняется."""
    track, clip = _require_clip(timeline, clip_id)

    new_in = clip.in_point if in_point is None else max(0.0, round(in_point, 4))
    new_out = clip.out_point if out_point is None else round(out_point, 4)

    if source_duration and clip.kind != "image":
        new_out = min(new_out, round(source_duration, 4))
    if new_out - new_in < MIN_CLIP_DURATION:
        raise TimelineError(f"Клип получается короче {MIN_CLIP_DURATION} с")

    previous = (clip.in_point, clip.out_point, clip.start)
    clip.in_point, clip.out_point = new_in, new_out
    if not keep_start and in_point is not None:
        # Подрезка «слева» с сохранением кадра под курсором: начало сдвигается на срезанное.
        clip.start = max(0.0, round(clip.start + (new_in - previous[0]) / clip.speed, 4))

    if clip.transition_in is not None and clip.transition_in.duration >= clip.duration:
        clip.in_point, clip.out_point, clip.start = previous
        raise TimelineError("Переход длиннее получившегося клипа — сначала clear_transition")
    if _overlaps(track, clip) is not None:
        clip.in_point, clip.out_point, clip.start = previous
        raise TimelineError("Подрезка накладывает клип на соседний")
    return clip


def split_clip(timeline: Timeline, clip_id: str, *, at: float) -> tuple[Clip, Clip]:
    """Разрезает клип в позиции таймлайна `at` (ТЗ п. 3.3)."""
    track, clip = _require_clip(timeline, clip_id)

    offset = at - clip.start
    if offset <= MIN_CLIP_DURATION or offset >= clip.duration - MIN_CLIP_DURATION:
        raise TimelineError("Разрез слишком близко к краю клипа")

    cut_source = clip.in_point + offset * clip.speed
    tail = Clip(
        source_id=clip.source_id,
        name=clip.name,
        kind=clip.kind,
        start=round(clip.start + offset, 4),
        in_point=round(cut_source, 4),
        out_point=clip.out_point,
        speed=clip.speed,
    )
    clip.out_point = round(cut_source, 4)
    track.clips.append(tail)
    return clip, tail


def set_speed(timeline: Timeline, clip_id: str, *, speed: float) -> Clip:
    track, clip = _require_clip(timeline, clip_id)
    if not MIN_SPEED <= speed <= MAX_SPEED:
        raise TimelineError(f"Скорость вне диапазона {MIN_SPEED}–{MAX_SPEED}")

    previous = clip.speed
    clip.speed = round(speed, 4)
    if clip.transition_in is not None and clip.transition_in.duration >= clip.duration:
        clip.speed = previous
        raise TimelineError("Переход длиннее получившегося клипа — сначала clear_transition")
    if _overlaps(track, clip) is not None:
        clip.speed = previous
        raise TimelineError("После изменения скорости клип налезает на соседний")
    return clip


def delete_clip(timeline: Timeline, clip_id: str) -> Clip:
    track, clip = _require_clip(timeline, clip_id)
    ordered = track.sorted_clips()
    idx = ordered.index(clip)
    if idx + 1 < len(ordered) and ordered[idx + 1].transition_in is not None:
        # Переход следующего клипа ссылался на удаляемый — иначе останется висящим.
        _untransition(track, ordered[idx + 1])
    track.clips.remove(clip)
    return clip


def close_gaps(timeline: Timeline, track_id: str) -> Track:
    """Сдвигает клипы дорожки встык — удобно после удаления из середины."""
    track = _require_track(timeline, track_id, "video")
    position = 0.0
    for clip in track.sorted_clips():
        clip.start = round(position, 4)
        position += clip.duration
    return track


def set_muted(timeline: Timeline, clip_id: str, *, muted: bool) -> Clip:
    """Выключает собственный звук клипа, чтобы он не наслаивался на музыку (ТЗ п. 3.3)."""
    _, clip = _require_clip(timeline, clip_id)
    clip.muted = muted
    return clip


def set_fade(
    timeline: Timeline, clip_id: str, *, fade_in: float | None = None, fade_out: float | None = None
) -> Clip:
    """Плавное появление/затухание клипа. Длиннее собственной длительности не бывает."""
    _, clip = _require_clip(timeline, clip_id)
    if fade_in is not None:
        if fade_in < 0:
            raise TimelineError("fade_in не может быть отрицательным")
        clip.fade_in = round(min(fade_in, clip.duration), 4)
    if fade_out is not None:
        if fade_out < 0:
            raise TimelineError("fade_out не может быть отрицательным")
        clip.fade_out = round(min(fade_out, clip.duration), 4)
    return clip


def _untransition(track: Track, clip: Clip) -> None:
    """Откатывает ripple-shift перехода `clip.transition_in`, если он есть — общая часть
    `clear_transition` и защитных сбросов при перемещении/удалении соседних клипов."""
    if clip.transition_in is None:
        return
    shift = clip.transition_in.duration
    ordered = track.sorted_clips()
    idx = ordered.index(clip)
    for later in ordered[idx:]:
        later.start = round(later.start + shift, 4)
    clip.transition_in = None


def set_transition(timeline: Timeline, clip_id: str, *, kind: str, duration: float) -> Clip:
    """Переход (кроссфейд, dip to black) между этим клипом и предыдущим на той же
    видеодорожке. Клипы должны стоять встык. Переход укорачивает итоговый видеоряд на
    `duration` — последующие клипы дорожки сдвигаются влево, чтобы не осталось дыры."""
    track, clip = _require_clip(timeline, clip_id)
    if track.kind != "video":
        raise TimelineError("Переходы поддерживаются только на видеодорожке")
    if kind not in TRANSITION_KINDS:
        raise TimelineError(f"Неизвестный тип перехода: {kind}. Доступны: {', '.join(TRANSITION_KINDS)}")
    if duration <= 0:
        raise TimelineError("Длительность перехода должна быть больше нуля")

    _untransition(track, clip)   # если уже был переход — откатить и поставить новый поверх чистого состояния

    ordered = track.sorted_clips()
    idx = ordered.index(clip)
    if idx == 0:
        raise TimelineError("У первого клипа дорожки нет предыдущего — переход невозможен")
    prev = ordered[idx - 1]

    if abs(clip.start - prev.end) > ADJACENCY_TOLERANCE:
        raise TimelineError("Клипы должны стоять встык, чтобы поставить между ними переход")
    if duration >= min(prev.duration, clip.duration):
        raise TimelineError("Переход не может быть длиннее любого из соседних клипов")

    clip.transition_in = Transition(kind=kind, duration=round(duration, 4))  # type: ignore[arg-type]
    shift = round(duration, 4)
    for later in ordered[idx:]:
        later.start = round(later.start - shift, 4)
    return clip


def clear_transition(timeline: Timeline, clip_id: str) -> Clip:
    """Убирает переход с клипа, возвращая сдвинутые ripple-эффектом клипы на место."""
    track, clip = _require_clip(timeline, clip_id)
    _untransition(track, clip)
    return clip


def add_text_clip(
    timeline: Timeline,
    *,
    text: str,
    duration: float,
    start: float | None = None,
    font_size: int = 48,
    position: str = "bottom",
    animation: str = "fade",
) -> Clip:
    """Добавляет текстовый слой на дорожку `text_1` (создаётся при первом обращении —
    в проектах, сохранённых до появления текстовых слоёв, её ещё нет)."""
    if not text.strip():
        raise TimelineError("Пустой текст")
    if duration < MIN_CLIP_DURATION:
        raise TimelineError(f"Текст короче {MIN_CLIP_DURATION} с не имеет смысла")
    if position not in TEXT_POSITIONS:
        raise TimelineError(f"Неизвестная позиция: {position}. Доступны: {', '.join(TEXT_POSITIONS)}")
    if animation not in ("none", "fade"):
        raise TimelineError(f"Неизвестная анимация: {animation}")

    track = _ensure_track(timeline, "text_1", "text", "Текст")
    clip = Clip(
        kind="text",
        name=text[:40],
        text=text,
        font_size=font_size,
        position=position,  # type: ignore[arg-type]
        animation=animation,  # type: ignore[arg-type]
        start=_append_position(track) if start is None else start,
        in_point=0.0,
        out_point=round(duration, 4),
    )

    if (other := _overlaps(track, clip)) is not None:
        clip.start = _append_position(track)
        if _overlaps(track, clip) is not None:
            raise TimelineError(f"Позиция занята текстом «{other.name}»")

    track.clips.append(clip)
    return clip


def set_text_properties(
    timeline: Timeline,
    clip_id: str,
    *,
    text: str | None = None,
    font_size: int | None = None,
    position: str | None = None,
    animation: str | None = None,
) -> Clip:
    """Правит текст/оформление уже добавленного текстового клипа (без пересоздания)."""
    _, clip = _require_clip(timeline, clip_id)
    if clip.kind != "text":
        raise TimelineError("Это не текстовый клип")

    if text is not None:
        if not text.strip():
            raise TimelineError("Пустой текст")
        clip.text = text
        clip.name = text[:40]
    if font_size is not None:
        clip.font_size = font_size
    if position is not None:
        if position not in TEXT_POSITIONS:
            raise TimelineError(f"Неизвестная позиция: {position}. Доступны: {', '.join(TEXT_POSITIONS)}")
        clip.position = position  # type: ignore[assignment]
    if animation is not None:
        if animation not in ("none", "fade"):
            raise TimelineError(f"Неизвестная анимация: {animation}")
        clip.animation = animation  # type: ignore[assignment]
    return clip
