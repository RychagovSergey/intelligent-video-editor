"""Конфигурация приложения: значения из .env + значения по умолчанию."""
from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def expand(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- хранилище медиа (Р-11) ---
    #: Пусто — хранилище ещё не выбрано; папки добавляются из UI или через .env.
    media_roots: str = ""

    # --- анализ (Р-9) ---
    ollama_base_url: str = "http://localhost:11434"
    vl_model: str = "qwen2.5vl:7b"
    vl_model_fast: str = "qwen2.5vl:3b"
    frame_sample_seconds: float = 2.0
    #: Предел кадров на одно видео: определяет фактический шаг выборки (см. ffmpeg/frames.py).
    max_frames: int = 5
    #: Кадры и фото приводятся к этой стороне перед отправкой в модель.
    model_image_max_side: int = 640

    # --- агент (Р-10) ---
    agent_provider: str = "openai"
    custom_base_url: str = ""
    custom_api_key: str = ""
    custom_chat_model: str = ""
    openai_api_key: str = ""
    openai_chat_model: str = ""
    openai_embed_model: str = ""
    #: Пусто — параметр не отправляется (старые модели его не знают и вернут 400).
    #: У части «рассуждающих» моделей (o-серия, GPT-5 и т. п.) function tools в
    #: /v1/chat/completions работают только с reasoning_effort="none" — сама модель
    #: этого не подсказывает без явной настройки.
    openai_reasoning_effort: str = ""
    #: Предел шагов агента за один запрос: шаг — это ответ модели с вызовами инструментов.
    max_steps_agent: int = 100
    #: Бюджет токенов на один ответ модели (max_tokens/max_completion_tokens). У моделей
    #: с рассуждениями (OpenAI o-серия, GPT-5 и т. п.) скрытые токены рассуждений тоже
    #: считаются из этого бюджета — при нехватке модель возвращает пустой ответ без единого
    #: вызова инструмента, и со стороны это выглядит как «агент застрял».
    max_tokens_agent: int = 4000
    #: Столько шагов подряд без изменений в таймлайне — и агенту шлют явную подсказку
    #: действовать, а не продолжать искать материал (см. agent/runner.py).
    stall_nudge_threshold: int = 15

    # --- бинарники (Р-5) ---
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    ffplay_path: str = ""

    # --- служебное ---
    #: Пусто — кеш лежит в `data` внутри первого корня хранилища (см. paths.py).
    data_dir_override: str = Field(default="", alias="DATA_DIR")

    @field_validator(
        "frame_sample_seconds", "model_image_max_side", "max_frames", "max_steps_agent",
        "max_tokens_agent", "stall_nudge_threshold",
        mode="before",
    )
    @classmethod
    def _blank_to_default(cls, v: object, info) -> object:
        defaults = {
            "frame_sample_seconds": 2.0, "model_image_max_side": 640,
            "max_frames": 5, "max_steps_agent": 100,
            "max_tokens_agent": 4000, "stall_nudge_threshold": 15,
        }
        return defaults[info.field_name] if v in ("", None) else v

    @property
    def default_media_roots(self) -> list[Path]:
        """Корни из .env: несколько — через запятую, `~` разворачивается."""
        return [expand(p) for p in (s.strip() for s in self.media_roots.split(",")) if p]

    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    def binary(self, name: str) -> str | None:
        """Путь к ffmpeg/ffprobe/ffplay: сначала из .env, затем из PATH."""
        override = getattr(self, f"{name}_path", "") or ""
        if override:
            return override if Path(override).exists() else None
        return shutil.which(name)


settings = Settings()
