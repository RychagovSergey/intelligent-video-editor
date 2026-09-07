"""Клиент внешней LLM с поддержкой вызова инструментов (ТЗ п. 4.5, решения Р-3 и Р-10).

Оба провайдера — OpenAI-совместимые, поэтому различаются только адресом, ключом
и именем модели. Ключи берутся из `.env` и никогда не попадают в логи и ответы API.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger(__name__)

TIMEOUT = 180.0


class AgentUnavailable(RuntimeError):
    """Провайдер не настроен или не отвечает."""


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str
    model: str
    #: У «размышляющих» моделей (GLM) без этого весь бюджет токенов уходит в рассуждения.
    disable_thinking: bool = False
    #: OpenAI отклоняет `max_tokens` у части моделей ("Unsupported parameter") и требует
    #: `max_completion_tokens` — это чисто разница в имени параметра API, не в модели.
    token_param: str = "max_tokens"
    #: Часть OpenAI-моделей с рассуждениями не даёт использовать function tools без
    #: reasoning_effort="none" в /v1/chat/completions; другие модели вообще не знают
    #: такого параметра и вернут 400 на любое его значение — поэтому по умолчанию не
    #: отправляем, включается явно через .env (OPENAI_REASONING_EFFORT).
    reasoning_effort: str | None = None


def current_provider() -> Provider:
    if settings.agent_provider == "openai":
        provider = Provider(
            name="openai",
            base_url="https://api.openai.com/v1",
            api_key=settings.openai_api_key,
            model=settings.openai_chat_model,
            token_param="max_completion_tokens",
            reasoning_effort=settings.openai_reasoning_effort or None,
        )
    else:
        provider = Provider(
            name="custom",
            base_url=settings.custom_base_url.rstrip("/"),
            api_key=settings.custom_api_key,
            model=settings.custom_chat_model,
            disable_thinking=True,
        )
    if not provider.api_key or not provider.model or not provider.base_url:
        raise AgentUnavailable(
            f"Провайдер «{provider.name}» не настроен: заполните ключ и модель в .env"
        )
    return provider


def chat(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    *,
    provider: Provider | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Один запрос к модели. Возвращает `message` из ответа (с tool_calls, если они есть)."""
    provider = provider or current_provider()
    token_budget = max_tokens if max_tokens is not None else settings.max_tokens_agent
    payload: dict[str, Any] = {
        "model": provider.model,
        "messages": messages,
        provider.token_param: token_budget,
        "temperature": 0.2,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if provider.disable_thinking:
        payload["thinking"] = {"type": "disabled"}
    if provider.reasoning_effort:
        payload["reasoning_effort"] = provider.reasoning_effort

    try:
        response = httpx.post(
            f"{provider.base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {provider.api_key}"},
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise AgentUnavailable(f"Не удалось обратиться к модели: {exc}") from exc

    if response.status_code >= 400:
        # Текст ошибки провайдера полезен, но ключ в него попасть не должен.
        raise AgentUnavailable(f"Модель вернула {response.status_code}: {response.text[:300]}")

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise AgentUnavailable(f"Пустой ответ модели: {str(data)[:200]}")
    return choices[0].get("message") or {}
