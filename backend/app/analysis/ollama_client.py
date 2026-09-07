"""Клиент Ollama для локальной VL-модели (ТЗ п. 4.2)."""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import settings

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 600.0   # холодный старт 7B-модели на M1 занимает до нескольких минут


class OllamaError(RuntimeError):
    """Модель недоступна или ответила ошибкой (ТЗ п. 5)."""


@dataclass
class ChatResult:
    content: str
    eval_count: int = 0
    duration: float = 0.0


def _encode(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


class OllamaClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")

    def available_models(self) -> list[str]:
        try:
            with httpx.Client(timeout=CONNECT_TIMEOUT) as client:
                response = client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                return [m["name"] for m in response.json().get("models", [])]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise OllamaError(f"Ollama недоступна на {self.base_url}: {exc}") from exc

    def resolve_model(self, requested: str) -> str:
        """Возвращает установленную модель; если запрошенной нет — падает обратно на основную."""
        models = self.available_models()
        if requested in models:
            return requested
        # `qwen2.5vl:7b` и `qwen2.5vl` — один и тот же образ с разными тегами.
        stem = requested.split(":")[0]
        same_family = [m for m in models if m.split(":")[0] == stem]
        if same_family:
            log.warning("Модель %s не установлена, используется %s", requested, same_family[0])
            return same_family[0]
        raise OllamaError(
            f"Модель {requested} не установлена. Загрузите её: ollama pull {requested}"
        )

    def chat(
        self,
        model: str,
        prompt: str,
        images: list[Path] | None = None,
        *,
        max_tokens: int = 200,
        temperature: float = 0.1,
    ) -> ChatResult:
        message: dict = {"role": "user", "content": prompt}
        if images:
            message["images"] = [_encode(p) for p in images]

        payload = {
            "model": model,
            "messages": [message],
            "stream": False,
            "format": "json",           # Ollama сама следит, чтобы ответ был валидным JSON
            # У «размышляющих» моделей (qwen3.5 и подобных) весь вывод иначе уходит
            # в message.thinking, а content остаётся пустым. Модели без режима
            # размышления этот параметр просто игнорируют.
            "think": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }

        try:
            with httpx.Client(timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT)) as client:
                response = client.post(f"{self.base_url}/api/chat", json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            raise OllamaError(f"Модель не ответила за {READ_TIMEOUT:.0f} с") from exc
        except httpx.HTTPStatusError as exc:
            raise OllamaError(f"Ollama вернула {exc.response.status_code}: {exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"Ollama недоступна: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise OllamaError(f"Неразборчивый ответ Ollama: {exc}") from exc

        message_out = data.get("message") or {}
        content = message_out.get("content", "")
        if not content:
            if message_out.get("thinking"):
                raise OllamaError(
                    f"Модель {model} ответила только размышлением — увеличьте лимит токенов "
                    "или выберите модель без режима размышления"
                )
            if data.get("done_reason") == "length":
                raise OllamaError("Ответ модели оборван по лимиту токенов")
            raise OllamaError("Модель вернула пустой ответ")
        return ChatResult(
            content=content,
            eval_count=data.get("eval_count", 0),
            duration=data.get("total_duration", 0) / 1e9,
        )
