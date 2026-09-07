"""Цикл агента: запрос пользователя → вызовы инструментов → изменённый таймлайн (ТЗ п. 3.5)."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT, settings
from ..db import session_scope
from ..timeline import store
from .client import AgentUnavailable, chat, current_provider
from .tools import TOOL_SPECS, Context, execute

log = logging.getLogger(__name__)

INSTRUCTIONS_FILE = PROJECT_ROOT / "EDITOR_AGENT.md"
FALLBACK_PROMPT = "Ты собираешь черновой видеомонтаж через доступные инструменты. Отвечай по-русски."

#: Инструменты, которые ничего не меняют в таймлайне — только чтение/поиск.
#: Всё, чего нет в этом списке (add_clip, set_transition, close_gaps, ...), засчитывается
#: как прогресс и сбрасывает счётчик «застревания» ниже.
READ_ONLY_TOOLS = frozenset({
    "search_media", "list_media", "get_media_details",
    "get_timeline", "get_audio_events", "get_audio_bpm",
})
#: Порог берётся из .env (STALL_NUDGE_THRESHOLD) — столько шагов подряд без единого
#: изменения таймлайна, и модели нужен явный толчок. Полагаться только на текст
#: инструкции ненадёжно: модель по факту зацикливалась на поиске материала даже после
#: того, как EDITOR_AGENT.md прямо просил чередовать поиск и add_clip — нужна
#: программная подстраховка, а не только формулировка промпта.
STALL_NUDGE = (
    "Это уже {n} шагов подряд без единого изменения таймлайна — только поиск и чтение. "
    "Хватит искать «ещё лучше»: возьми то, что уже нашлось по прошлым search_media/"
    "get_media_details, и прямо сейчас вызови add_clip хотя бы для одного клипа — потом "
    "можно продолжить добавлять. Не выдумывай source_id — бери только те, что реально "
    "приходили в ответах инструментов."
)


def instructions() -> str:
    """Системный промпт берётся из EDITOR_AGENT.md рядом с проектом (ТЗ п. 3.5, деривация Р-17)."""
    try:
        return INSTRUCTIONS_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("EDITOR_AGENT.md не прочитан (%s), беру запасной промпт", exc)
        return FALLBACK_PROMPT


class AgentJob:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

        self.state: str = "idle"      # idle | running | done | error | cancelled
        self.project_id: int | None = None
        self.prompt: str | None = None
        self.model: str | None = None
        self.step: int = 0
        self.log: list[dict[str, Any]] = []
        self.answer: str | None = None
        self.error: str | None = None
        self.finished_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, project_id: int, prompt: str) -> bool:
        with self._lock:
            if self.running:
                return False
            self._cancel.clear()
            self.state = "running"
            self.project_id = project_id
            self.prompt = prompt
            self.step = 0
            self.log = []
            self.answer = None
            self.error = None
            self.finished_at = None
            self._thread = threading.Thread(target=self._run, args=(project_id, prompt), daemon=True)
            self._thread.start()
            return True

    def cancel(self) -> bool:
        if not self.running:
            return False
        self._cancel.set()
        return True

    def _note(self, kind: str, text: str, detail: Any = None) -> None:
        self.log.append({"kind": kind, "text": text, "detail": detail})
        del self.log[:-60]

    def _run(self, project_id: int, prompt: str) -> None:
        try:
            provider = current_provider()
            self.model = provider.model

            # Предел шагов берётся из .env (MAX_STEPS_AGENT): дальше модель обычно ходит по кругу.
            max_steps = settings.max_steps_agent
            stall_nudge_threshold = settings.stall_nudge_threshold

            with session_scope() as db:
                timeline = store.load(db, project_id)
                ctx = Context(db=db, project_id=project_id, timeline=timeline)

                messages: list[dict[str, Any]] = [
                    {"role": "system", "content": instructions()},
                    {"role": "user", "content": prompt},
                ]
                stall_steps = 0

                for step in range(1, max_steps + 1):
                    if self._cancel.is_set():
                        self.state = "cancelled"
                        ctx.save()
                        return

                    self.step = step
                    message = chat(messages, TOOL_SPECS, provider=provider)
                    calls = message.get("tool_calls") or []
                    messages.append({
                        "role": "assistant",
                        "content": message.get("content") or "",
                        **({"tool_calls": calls} if calls else {}),
                    })

                    if not calls:
                        content = (message.get("content") or "").strip()
                        if content:
                            self.answer = content
                        elif ctx.changed:
                            # Модель не оставила текстового ответа, но правки в таймлайн
                            # реально были — по крайней мере не выдумываем текст за неё.
                            self.answer = "Монтаж собран."
                        else:
                            # Ни одного изменения в таймлайне и ни одного слова ответа —
                            # нельзя утверждать, что монтаж готов (иначе пользователь
                            # решит, что всё сделано, хотя таймлайн не тронут).
                            self.answer = (
                                "Модель не внесла ни одного изменения в таймлайн и не "
                                "оставила пояснения. Похоже, она застряла на подборе "
                                "материала — попробуйте сузить запрос или повторить его."
                            )
                        self._note("answer", self.answer)
                        break

                    step_progressed = False
                    for call in calls:
                        function = call.get("function") or {}
                        name = function.get("name", "")
                        result = execute(ctx, name, function.get("arguments") or "{}")
                        if name not in READ_ONLY_TOOLS and "error" not in result:
                            step_progressed = True
                        self._note(
                            "error" if "error" in result else "tool",
                            f"{name}: {result.get('error') or _short(result)}",
                            {"tool": name, "arguments": function.get("arguments")},
                        )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call.get("id", ""),
                            "content": json.dumps(result, ensure_ascii=False)[:4000],
                        })

                    if step_progressed:
                        stall_steps = 0
                    else:
                        stall_steps += 1
                        if stall_steps == stall_nudge_threshold:
                            nudge = STALL_NUDGE.format(n=stall_steps)
                            messages.append({"role": "user", "content": nudge})
                            self._note("tool", f"подсказка модели: {stall_steps} шагов без изменений таймлайна")
                            stall_steps = 0
                else:
                    self._note("error", f"Остановился после {max_steps} шагов")
                    self.answer = (
                        "Не уложился в отведённое число шагов, монтаж собран частично."
                        if ctx.changed else
                        "Не уложился в отведённое число шагов и не успел ничего добавить "
                        "на таймлайн — весь бюджет шагов ушёл на подбор материала."
                    )

                # Всё, что агент успел сделать, попадает в проект одной правкой —
                # пользователь может отменить её одним ⌘Z.
                ctx.save()

            self.state = "done"

        except AgentUnavailable as exc:
            self.error = str(exc)
            self.state = "error"
            self._note("error", self.error)
        except Exception as exc:  # noqa: BLE001
            log.exception("Агент упал")
            self.error = f"{type(exc).__name__}: {exc}"
            self.state = "error"
            self._note("error", self.error)
        finally:
            self.finished_at = datetime.now(timezone.utc)

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "project_id": self.project_id,
            "prompt": self.prompt,
            "model": self.model,
            "step": self.step,
            "max_steps": settings.max_steps_agent,
            "log": self.log[-30:],
            "answer": self.answer,
            "error": self.error,
        }


def _short(result: dict) -> str:
    if "results" in result:
        return f"найдено {len(result['results'])}"
    if "clip" in result:
        clip = result["clip"]
        return f"{clip['name'][:28]} {clip['start']}–{round(clip['start'] + clip['duration'], 2)} с"
    if "tracks" in result:
        total = sum(len(t["clips"]) for t in result["tracks"])
        return f"клипов на таймлайне: {total}"
    return json.dumps(result, ensure_ascii=False)[:120]


agent_job = AgentJob()
