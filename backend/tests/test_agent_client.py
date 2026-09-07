"""Клиент внешней LLM: выбор провайдера и синтаксис запроса (ТЗ п. 4.5)."""
from __future__ import annotations

import httpx
import pytest

from backend.app.agent import client
from backend.app.config import settings


class _FakeResponse:
    status_code = 200

    def json(self) -> dict:
        return {"choices": [{"message": {"content": "ok"}}]}


@pytest.fixture
def captured_payload(monkeypatch):
    box: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        box["url"] = url
        box["payload"] = json
        return _FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    return box


def test_openai_provider_uses_max_completion_tokens(monkeypatch, captured_payload) -> None:
    """OpenAI отклоняет `max_tokens` у части моделей — нужен `max_completion_tokens`."""
    monkeypatch.setattr(settings, "agent_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "openai_chat_model", "gpt-5-mini")

    client.chat([{"role": "user", "content": "hi"}])

    assert "max_completion_tokens" in captured_payload["payload"]
    assert "max_tokens" not in captured_payload["payload"]


def test_custom_provider_still_uses_max_tokens(monkeypatch, captured_payload) -> None:
    """Сторонние OpenAI-совместимые бэкенды (GLM и т. п.) ждут старое имя параметра."""
    monkeypatch.setattr(settings, "agent_provider", "custom")
    monkeypatch.setattr(settings, "custom_base_url", "http://localhost:1234/v1")
    monkeypatch.setattr(settings, "custom_api_key", "key")
    monkeypatch.setattr(settings, "custom_chat_model", "glm")

    client.chat([{"role": "user", "content": "hi"}])

    assert "max_tokens" in captured_payload["payload"]
    assert "max_completion_tokens" not in captured_payload["payload"]


def test_max_tokens_defaults_to_settings_value(monkeypatch, captured_payload) -> None:
    monkeypatch.setattr(settings, "agent_provider", "custom")
    monkeypatch.setattr(settings, "custom_base_url", "http://localhost:1234/v1")
    monkeypatch.setattr(settings, "custom_api_key", "key")
    monkeypatch.setattr(settings, "custom_chat_model", "glm")
    monkeypatch.setattr(settings, "max_tokens_agent", 9999)

    client.chat([{"role": "user", "content": "hi"}])

    assert captured_payload["payload"]["max_tokens"] == 9999


def test_reasoning_effort_omitted_by_default(monkeypatch, captured_payload) -> None:
    """Пусто по умолчанию — модели без reasoning_effort вернули бы 400 на любое значение."""
    monkeypatch.setattr(settings, "agent_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "openai_chat_model", "gpt-4o")
    monkeypatch.setattr(settings, "openai_reasoning_effort", "")

    client.chat([{"role": "user", "content": "hi"}])

    assert "reasoning_effort" not in captured_payload["payload"]


def test_reasoning_effort_sent_when_configured(monkeypatch, captured_payload) -> None:
    """Часть моделей с рассуждениями требует явный reasoning_effort='none' для tool calls."""
    monkeypatch.setattr(settings, "agent_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "openai_chat_model", "gpt-5.6-terra")
    monkeypatch.setattr(settings, "openai_reasoning_effort", "none")

    client.chat([{"role": "user", "content": "hi"}])

    assert captured_payload["payload"]["reasoning_effort"] == "none"


def test_custom_provider_never_sends_reasoning_effort(monkeypatch, captured_payload) -> None:
    monkeypatch.setattr(settings, "agent_provider", "custom")
    monkeypatch.setattr(settings, "custom_base_url", "http://localhost:1234/v1")
    monkeypatch.setattr(settings, "custom_api_key", "key")
    monkeypatch.setattr(settings, "custom_chat_model", "glm")
    monkeypatch.setattr(settings, "openai_reasoning_effort", "none")   # не должно влиять

    client.chat([{"role": "user", "content": "hi"}])

    assert "reasoning_effort" not in captured_payload["payload"]


def test_provider_error_status_raises_agent_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "openai_chat_model", "gpt-5-mini")

    class BadResponse:
        status_code = 400
        text = '{"error": {"message": "Unsupported parameter"}}'

    monkeypatch.setattr(httpx, "post", lambda *a, **k: BadResponse())

    with pytest.raises(client.AgentUnavailable, match="400"):
        client.chat([{"role": "user", "content": "hi"}])
