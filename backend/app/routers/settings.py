"""Панель настроек, проверка окружения и логи (этап 8, ТЗ пп. 3.7, 5)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..analysis.ollama_client import OllamaClient, OllamaError
from ..config import ENV_FILE, Settings, expand, reload_settings, settings
from ..logbuffer import buffer
from ..settings_file import write_env

router = APIRouter(prefix="/api", tags=["settings"])

#: Значение, которым GET подменяет ключи API. Если оно же вернулось в PUT —
#: пользователь поле не трогал, и ключ в .env не переписывается.
SECRET_MASK = "••••••••"

Kind = Literal["text", "secret", "int", "float", "select"]


@dataclass(frozen=True)
class Spec:
    key: str                      # имя переменной в .env
    label: str
    kind: Kind = "text"
    hint: str = ""
    restart: bool = False          # значение читается при импорте — нужен перезапуск
    options: list[str] = field(default_factory=list)
    placeholder: str = ""

    @property
    def attr(self) -> str:
        return "data_dir_override" if self.key == "DATA_DIR" else self.key.lower()


@dataclass(frozen=True)
class Group:
    id: str
    title: str
    fields: list[Spec]


#: Каталог полей панели. `MEDIA_ROOTS` сюда намеренно не входит: папки хранилища
#: правятся в своём диалоге и живут в базе (Р-11).
GROUPS: list[Group] = [
    Group("analysis", "Анализ", [
        Spec("OLLAMA_BASE_URL", "Адрес Ollama", placeholder="http://localhost:11434"),
        Spec("VL_MODEL", "Основная VL-модель", "select",
             hint="На Apple Silicon — сборки с суффиксом -mlx; на Linux/Windows — без него."),
        Spec("VL_MODEL_FAST", "Быстрая VL-модель", "select",
             hint="Режим «быстрая модель» в интерфейсе: менее точная, но быстрее."),
        Spec("FRAME_SAMPLE_SECONDS", "Шаг выборки кадров, с", "float",
             hint="Желаемый интервал между кадрами видео при анализе."),
        Spec("MAX_FRAMES", "Предел кадров на видео", "int",
             hint="Если кадров по шагу выходит больше — шаг растягивается (Р-16)."),
        Spec("MODEL_IMAGE_MAX_SIDE", "Сторона картинки для модели, px", "int",
             hint="Кадры и фото сжимаются до этой стороны перед отправкой в модель (Р-13)."),
    ]),
    Group("agent", "Агент", [
        Spec("AGENT_PROVIDER", "Провайдер", "select", options=["openai", "custom"]),
        Spec("OPENAI_API_KEY", "OpenAI API key", "secret",
             hint="Нужен и для эмбеддингов семантического поиска, даже если чат идёт через custom."),
        Spec("OPENAI_CHAT_MODEL", "Модель чата OpenAI", placeholder="gpt-4.1-mini"),
        Spec("OPENAI_EMBED_MODEL", "Модель эмбеддингов", placeholder="text-embedding-3-small"),
        Spec("OPENAI_REASONING_EFFORT", "reasoning_effort", "select",
             options=["", "none", "low", "medium", "high"],
             hint="Для рассуждающих моделей OpenAI function tools работают только с none; "
                  "пусто — параметр не отправляется."),
        Spec("CUSTOM_BASE_URL", "Custom: base URL", placeholder="https://api.z.ai/api/paas/v4"),
        Spec("CUSTOM_API_KEY", "Custom: API key", "secret"),
        Spec("CUSTOM_CHAT_MODEL", "Custom: модель", placeholder="glm-4.6"),
        Spec("MAX_STEPS_AGENT", "Предел шагов агента", "int",
             hint="Ролику на 20–25 клипов хватает полусотни; на исчерпании монтаж остаётся частичным."),
        Spec("MAX_TOKENS_AGENT", "Бюджет токенов на ответ", "int",
             hint="У рассуждающих моделей скрытые токены считаются отсюда же."),
        Spec("STALL_NUDGE_THRESHOLD", "Подсказка после N шагов без правок", "int"),
    ]),
    Group("binaries", "Бинарники и кеш", [
        Spec("FFMPEG_PATH", "ffmpeg", hint="Пусто — ищется в PATH.", placeholder="/opt/homebrew/bin/ffmpeg"),
        Spec("FFPROBE_PATH", "ffprobe", placeholder="/opt/homebrew/bin/ffprobe"),
        Spec("FFPLAY_PATH", "ffplay", placeholder="/opt/homebrew/bin/ffplay"),
        Spec("DATA_DIR", "Папка кеша", restart=True,
             hint="База индекса, метаданные, миниатюры, прокси. Пусто — `data` внутри первого корня "
                  "хранилища (Р-15).",
             placeholder="~/Library/Application Support/IntelligentVideoEditor"),
    ]),
]

SPECS: dict[str, Spec] = {spec.key: spec for group in GROUPS for spec in group.fields}
_DEFAULTS = Settings.model_construct()


class SettingsIn(BaseModel):
    values: dict[str, Any]


def _current(spec: Spec) -> Any:
    value = getattr(settings, spec.attr)
    if spec.kind == "secret":
        return SECRET_MASK if value else ""
    return value


def _field_view(spec: Spec, installed_models: list[str]) -> dict:
    options = spec.options
    if spec.key in ("VL_MODEL", "VL_MODEL_FAST"):
        # Текущее значение — первым, даже если модель ещё не скачана: иначе select
        # молча подменил бы его на первое установленное.
        current = getattr(settings, spec.attr)
        options = list(dict.fromkeys([current, *installed_models]))
    return {
        "key": spec.key,
        "label": spec.label,
        "kind": spec.kind,
        "hint": spec.hint,
        "restart": spec.restart,
        "options": options,
        "placeholder": spec.placeholder,
        "value": _current(spec),
        "default": "" if spec.kind == "secret" else getattr(_DEFAULTS, spec.attr),
        # Переменная окружения процесса сильнее .env — правка из панели не подействует.
        "overridden": spec.key in os.environ,
    }


def _view(installed_models: list[str] | None = None) -> dict:
    if installed_models is None:
        try:
            installed_models = OllamaClient().available_models()
        except OllamaError:
            installed_models = []
    return {
        "env_file": str(ENV_FILE),
        "env_exists": ENV_FILE.exists(),
        "groups": [
            {"id": g.id, "title": g.title, "fields": [_field_view(s, installed_models) for s in g.fields]}
            for g in GROUPS
        ],
    }


@router.get("/settings")
def get_settings() -> dict:
    return _view()


def _coerce(spec: Spec, raw: Any) -> str:
    """Проверяет значение по типу поля и возвращает строку для `.env`."""
    text = "" if raw is None else str(raw).strip()
    if text == "":
        return ""
    try:
        if spec.kind == "int":
            if int(text) <= 0:
                raise ValueError
        elif spec.kind == "float":
            if float(text.replace(",", ".")) <= 0:
                raise ValueError
            text = text.replace(",", ".")
    except ValueError:
        raise HTTPException(400, detail=f"«{spec.label}»: ожидается положительное число") from None
    if spec.kind == "select" and spec.options and text not in spec.options:
        raise HTTPException(400, detail=f"«{spec.label}»: недопустимое значение {text!r}")
    if spec.key.endswith("_PATH") and not Path(text).expanduser().exists():
        raise HTTPException(400, detail=f"«{spec.label}»: файл {text} не найден")
    return text


@router.put("/settings")
def put_settings(payload: SettingsIn) -> dict:
    """Переписывает значения в `.env` и перечитывает их без перезапуска.

    Ключи, вернувшиеся маской, не трогаются: поле в панели показывалось замаскированным,
    и пользователь его не менял.
    """
    updates: dict[str, str] = {}
    restart_required = False
    for key, raw in payload.values.items():
        spec = SPECS.get(key)
        if spec is None:
            raise HTTPException(400, detail=f"Неизвестная настройка: {key}")
        if spec.kind == "secret" and raw == SECRET_MASK:
            continue
        value = _coerce(spec, raw)
        before = getattr(settings, spec.attr)
        if str(before) == value or (before in ("", None) and value == ""):
            continue
        updates[key] = value
        restart_required = restart_required or spec.restart

    if updates:
        try:
            write_env(updates)
        except OSError as exc:
            raise HTTPException(500, detail=f"Не удалось записать {ENV_FILE}: {exc}") from exc
        reload_settings()

    view = _view()
    view["saved"] = sorted(updates)
    view["restart_required"] = restart_required
    return view


class Check(BaseModel):
    id: str
    level: Literal["ok", "warn", "error"]
    text: str
    hint: str = ""


@router.get("/settings/check", response_model=list[Check])
def check_environment() -> list[Check]:
    """Сквозная проверка окружения (ТЗ п. 5): что именно не настроено и что с этим делать.

    Показывается баннером при старте и внутри панели настроек — вместо того чтобы
    пользователь узнавал о проблеме из ошибки первого же анализа или экспорта.
    """
    checks: list[Check] = []

    for name, required in (("ffmpeg", True), ("ffprobe", True), ("ffplay", False)):
        path = settings.binary(name)
        if path:
            checks.append(Check(id=name, level="ok", text=f"{name}: {path}"))
        elif required:
            checks.append(Check(
                id=name, level="error", text=f"{name} не найден",
                hint=f"Установите ffmpeg (brew install ffmpeg) или укажите путь в поле «{name}».",
            ))
        else:
            checks.append(Check(
                id=name, level="warn", text="ffplay не найден",
                hint="Без него не работает предпросмотр отдельным окном; встроенный плеер работает.",
            ))

    try:
        models = OllamaClient().available_models()
    except OllamaError as exc:
        checks.append(Check(
            id="ollama", level="error", text=str(exc),
            hint="Запустите Ollama (ollama serve) или поправьте адрес в настройках.",
        ))
    else:
        checks.append(Check(id="ollama", level="ok", text=f"Ollama: {settings.ollama_base_url}"))
        for key, model in (("vl_model", settings.vl_model), ("vl_model_fast", settings.vl_model_fast)):
            if model in models:
                checks.append(Check(id=key, level="ok", text=f"Модель {model} установлена"))
            else:
                checks.append(Check(
                    id=key, level="error" if key == "vl_model" else "warn",
                    text=f"Модель {model} не установлена",
                    hint=f"ollama pull {model}",
                ))

    provider = settings.agent_provider
    key_present = bool(settings.custom_api_key if provider == "custom" else settings.openai_api_key)
    model_present = bool(settings.custom_chat_model if provider == "custom" else settings.openai_chat_model)
    if key_present and model_present:
        checks.append(Check(id="agent", level="ok", text=f"Агент: {provider}"))
    else:
        missing = "ключ API" if not key_present else "модель чата"
        checks.append(Check(
            id="agent", level="warn", text=f"Агент ({provider}): не задан(а) {missing}",
            hint="Без этого встроенный агент монтажа недоступен; ручной монтаж работает.",
        ))
    if not (settings.openai_api_key and settings.openai_embed_model):
        checks.append(Check(
            id="embeddings", level="warn", text="Семантический поиск не настроен",
            hint="Нужны OpenAI API key и модель эмбеддингов; без них search_media ищет по словам.",
        ))
    else:
        checks.append(Check(id="embeddings", level="ok", text=f"Эмбеддинги: {settings.openai_embed_model}"))

    if settings.data_dir_override:
        cache = expand(settings.data_dir_override)
        if not (cache.is_dir() and os.access(cache, os.W_OK)):
            checks.append(Check(
                id="data_dir", level="error", text=f"Папка кеша недоступна для записи: {cache}",
            ))
    return checks


@router.get("/logs")
def get_logs(after: int = 0, limit: int = Query(200, ge=1, le=1000)) -> dict:
    """Записи журнала после `after` — панель логов дочитывает только новое."""
    return {"entries": buffer.since(after, limit), "last_seq": buffer.last_seq}
