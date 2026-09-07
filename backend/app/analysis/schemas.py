"""Схемы meta.json и приведение ответа модели к ним (ТЗ п. 3.2)."""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

MAX_LIST = 8


def _as_list(value: Any) -> list[str]:
    """Модель может вернуть строку вместо списка — приводим к списку."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in re.split(r"[,;]", value) if p.strip()]
        return parts[:MAX_LIST]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()][:MAX_LIST]
    return [str(value)]


def _as_text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return "" if value is None else str(value).strip()


class ImageMeta(BaseModel):
    description: str = ""
    objects: list[str] = Field(default_factory=list)
    style: str = ""
    colors: list[str] = Field(default_factory=list)
    emotions: str = ""
    quality: str = ""

    @field_validator("objects", "colors", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _as_list(v)

    @field_validator("description", "style", "emotions", "quality", mode="before")
    @classmethod
    def _texts(cls, v: Any) -> str:
        return _as_text(v)


class SceneMeta(BaseModel):
    time: float = 0.0
    description: str = ""
    objects: list[str] = Field(default_factory=list)
    style: str = ""
    colors: list[str] = Field(default_factory=list)

    @field_validator("objects", "colors", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _as_list(v)

    @field_validator("description", "style", mode="before")
    @classmethod
    def _texts(cls, v: Any) -> str:
        return _as_text(v)


class SummaryMeta(BaseModel):
    summary: str = ""
    objects: list[str] = Field(default_factory=list)
    style: str = ""
    emotions: str = ""

    @field_validator("objects", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> list[str]:
        return _as_list(v)

    @field_validator("summary", "style", "emotions", mode="before")
    @classmethod
    def _texts(cls, v: Any) -> str:
        return _as_text(v)


class InvalidModelOutput(ValueError):
    """Ответ модели не удалось привести к схеме."""


def parse_json(raw: str) -> dict:
    """Разбирает ответ модели, прощая обёртку ```json и текст вокруг объекта."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise InvalidModelOutput(f"Ответ не содержит JSON: {raw[:200]}") from None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise InvalidModelOutput(f"Некорректный JSON: {exc}") from exc

    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict):
        raise InvalidModelOutput(f"Ожидался объект JSON, получено {type(data).__name__}")
    return data
