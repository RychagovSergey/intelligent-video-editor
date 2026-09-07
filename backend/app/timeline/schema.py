"""Схема проекта монтажа (ТЗ п. 3.3).

Версионированная модель: `version` позволяет менять формат, не ломая сохранённые проекты (Р-8).
В MVP используются одна видео- и одна аудиодорожка, но структура допускает несколько.
"""
from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

TIMELINE_VERSION = 1

DEFAULT_IMAGE_DURATION = 3.0    # длительность показа изображения (ТЗ п. 3.3)
MIN_CLIP_DURATION = 0.04        # один кадр при 25 fps
MIN_SPEED = 0.25
MAX_SPEED = 4.0

TrackKind = Literal["video", "audio", "text"]

#: Ключ модели → значение параметра `xfade=transition=...` (см. `ffmpeg -filters`).
#: Только эти два — «хотя бы crossfade» по заявке; whip pan среди встроенных переходов
#: xfade нет, это отдельный кастомный фильтр, вне объёма.
TRANSITION_KINDS = {"crossfade": "fade", "dip_to_black": "fadeblack"}

TEXT_POSITIONS = ("top", "center", "bottom")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Transition(BaseModel):
    kind: Literal["crossfade", "dip_to_black"] = "crossfade"
    duration: float = 0.5

    @field_validator("duration")
    @classmethod
    def _duration_non_negative(cls, v: float) -> float:
        return max(0.0, round(float(v), 4))


class Clip(BaseModel):
    id: str = Field(default_factory=lambda: new_id("clip"))
    source_id: int | None = None        # media_files.id; пусто у текстовых клипов (kind="text")
    name: str = ""                      # имя файла, чтобы UI не ходил в базу за каждым клипом
    kind: Literal["video", "image", "audio", "text"] = "video"

    start: float = 0.0                  # позиция на таймлайне, с
    in_point: float = 0.0               # начало фрагмента в исходнике, с
    out_point: float = DEFAULT_IMAGE_DURATION   # конец фрагмента в исходнике, с
    speed: float = 1.0
    muted: bool = False                 # не брать собственный звук клипа в микс
    fade_in: float = 0.0                # плавное появление, с (на собственной длительности клипа)
    fade_out: float = 0.0               # плавное затухание, с
    transition_in: Transition | None = None   # переход ИЗ предыдущего клипа дорожки В этот

    # --- только для kind="text" ---
    text: str | None = None
    font_size: int | None = None
    position: Literal["top", "center", "bottom"] | None = None
    animation: Literal["none", "fade"] | None = None

    @field_validator("start", "in_point")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        return max(0.0, round(float(v), 4))

    @field_validator("out_point")
    @classmethod
    def _positive(cls, v: float) -> float:
        return round(float(v), 4)

    @field_validator("speed")
    @classmethod
    def _speed_range(cls, v: float) -> float:
        return min(MAX_SPEED, max(MIN_SPEED, round(float(v), 4)))

    @field_validator("fade_in", "fade_out")
    @classmethod
    def _fade_non_negative(cls, v: float) -> float:
        return max(0.0, round(float(v), 4))

    @model_validator(mode="after")
    def _check_range(self) -> "Clip":
        if self.out_point - self.in_point < MIN_CLIP_DURATION:
            raise ValueError(
                f"Клип короче {MIN_CLIP_DURATION} с: in={self.in_point}, out={self.out_point}"
            )
        return self

    @property
    def source_duration(self) -> float:
        """Длительность взятого фрагмента в исходнике."""
        return self.out_point - self.in_point

    @property
    def duration(self) -> float:
        """Длительность на таймлайне с учётом скорости."""
        return round(self.source_duration / self.speed, 4)

    @property
    def end(self) -> float:
        return round(self.start + self.duration, 4)


class Track(BaseModel):
    id: str = Field(default_factory=lambda: new_id("track"))
    kind: TrackKind = "video"
    name: str = ""
    muted: bool = False
    clips: list[Clip] = Field(default_factory=list)

    def sorted_clips(self) -> list[Clip]:
        return sorted(self.clips, key=lambda c: c.start)

    @property
    def duration(self) -> float:
        return max((c.end for c in self.clips), default=0.0)


class Timeline(BaseModel):
    version: int = TIMELINE_VERSION
    fps: float = 30.0
    width: int = 1920
    height: int = 1080
    tracks: list[Track] = Field(default_factory=list)

    @property
    def duration(self) -> float:
        return max((t.duration for t in self.tracks), default=0.0)

    def track(self, track_id: str) -> Track | None:
        return next((t for t in self.tracks if t.id == track_id), None)

    def find_clip(self, clip_id: str) -> tuple[Track, Clip] | tuple[None, None]:
        for track in self.tracks:
            for clip in track.clips:
                if clip.id == clip_id:
                    return track, clip
        return None, None

    def first_track(self, kind: TrackKind) -> Track | None:
        return next((t for t in self.tracks if t.kind == kind), None)


def empty_timeline() -> Timeline:
    """Новый проект: видео, аудио и текстовая дорожка (ТЗ п. 3.3 + текстовые слои)."""
    return Timeline(
        tracks=[
            Track(id="video_1", kind="video", name="Видео"),
            Track(id="audio_1", kind="audio", name="Аудио"),
            Track(id="text_1", kind="text", name="Текст"),
        ]
    )
