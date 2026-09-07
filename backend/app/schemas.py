"""Схемы запросов и ответов API."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from .models import AnalysisStatus, MediaType, enum_value


class FolderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    path: str
    parent_id: int | None
    is_root: bool
    missing: bool
    file_count: int = 0
    has_children: bool = False


class MediaFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    folder_id: int
    filename: str
    path: str
    type: MediaType
    size: int
    modified: float
    has_meta: bool
    analysis_status: AnalysisStatus
    missing: bool
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    bitrate: int | None = None
    has_audio: bool = False
    has_cover_art: bool = False
    captured_at: datetime | None = None
    probe_error: str | None = None

    @field_validator("has_meta", "missing", "has_audio", "has_cover_art", mode="before")
    @classmethod
    def _null_is_false(cls, v: object) -> bool:
        """В старых базах колонки, добавленные позже, содержат NULL."""
        return bool(v)

    @field_serializer("type", "analysis_status")
    def _enum_value(self, v: MediaType | AnalysisStatus) -> str:
        return enum_value(v)


class MediaListOut(BaseModel):
    items: list[MediaFileOut]
    total: int
    offset: int
    limit: int


class MetaOut(BaseModel):
    file_id: int
    model: str | None = None
    updated_at: datetime | None = None
    meta: dict[str, Any] | None = None

    @staticmethod
    def from_cache(file_id: int, cache: Any | None) -> "MetaOut":
        if cache is None:
            return MetaOut(file_id=file_id)
        try:
            data = json.loads(cache.meta_json)
        except json.JSONDecodeError:
            data = None
        return MetaOut(file_id=file_id, model=cache.model, updated_at=cache.updated_at, meta=data)


class RootsIn(BaseModel):
    roots: list[str]


class RootInfo(BaseModel):
    path: str
    exists: bool
    is_dir: bool
    writable: bool   # можно ли писать сайдкары рядом с файлами (Р-1)


class RootsOut(BaseModel):
    roots: list[RootInfo]


class ScanStatusOut(BaseModel):
    state: str                     # idle | running | done | error
    phase: str | None = None       # scanning | probing
    started_at: datetime | None = None
    finished_at: datetime | None = None
    current_root: str | None = None
    current_file: str | None = None
    done: int = 0
    total: int = 0
    stats: dict[str, Any] | None = None
    error: str | None = None
