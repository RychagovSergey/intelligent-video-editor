"""Предпросмотр проекта: прокси-рендер + запуск ffplay (ТЗ пп. 3.4, 4.4)."""
from __future__ import annotations

import hashlib
import logging
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import session_scope
from .ffmpeg.compile import PREVIEW_PRESET, CompileError, build_command
from .ffmpeg.render import RenderCancelled, RenderFailed, play, render
from .ffmpeg.runner import BinaryMissing
from .paths import proxy_dir
from .timeline import store

log = logging.getLogger(__name__)

KEEP_PROXIES = 8        # старые прокси удаляем, чтобы папка не росла без предела


def proxy_path(project_id: int, timeline_json: str) -> Path:
    """Имя файла — хеш таймлайна: тот же монтаж не рендерится дважды."""
    digest = hashlib.sha1(f"{PREVIEW_PRESET.name}|{timeline_json}".encode()).hexdigest()[:16]
    return proxy_dir() / f"project{project_id}_{digest}.mp4"


def _cleanup() -> None:
    files = sorted(proxy_dir().glob("project*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in files[KEEP_PROXIES:]:
        stale.unlink(missing_ok=True)


class PreviewJob:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._player: subprocess.Popen | None = None

        self.state: str = "idle"        # idle | rendering | ready | playing | done | error | cancelled
        self.project_id: int | None = None
        self.done_seconds: float = 0.0
        self.total_seconds: float = 0.0
        self.output: str | None = None
        self.cached: bool = False
        self.error: str | None = None
        self.finished_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, project_id: int, *, launch_player: bool = False) -> bool:
        """`launch_player` открывает отдельное окно ffplay; иначе прокси играется в приложении."""
        with self._lock:
            if self.running:
                return False
            self._cancel.clear()
            self.state = "rendering"
            self.project_id = project_id
            self.done_seconds = self.total_seconds = 0.0
            self.output = None
            self.cached = False
            self.error = None
            self.finished_at = None
            self._thread = threading.Thread(
                target=self._run, args=(project_id, launch_player), daemon=True
            )
            self._thread.start()
            return True

    def cancel(self) -> bool:
        stopped = False
        if self.running:
            self._cancel.set()
            stopped = True
        if self._player and self._player.poll() is None:
            self._player.terminate()
            stopped = True
        return stopped

    def _run(self, project_id: int, launch_player: bool = False) -> None:
        target: Path | None = None
        try:
            proxy_dir().mkdir(parents=True, exist_ok=True)
            with session_scope() as db:
                timeline = store.load(db, project_id)
                target = proxy_path(project_id, timeline.model_dump_json())
                self.total_seconds = timeline.duration

                if target.exists():
                    # Таймлайн не менялся с прошлого раза — рендерить нечего.
                    self.cached = True
                    self.done_seconds = self.total_seconds
                else:
                    command = build_command(db, timeline, target, PREVIEW_PRESET)
                    self.total_seconds = command.duration
                    render(
                        command,
                        on_progress=lambda done, total: self._on_progress(done, total),
                        should_stop=self._cancel.is_set,
                    )

            self.output = str(target)
            if not launch_player:
                # Файл готов — дальше его играет <video> в самом приложении.
                self.state = "ready"
                return

            self.state = "playing"
            self._player = play(target, title=f"Предпросмотр — проект {project_id}")
            self._player.wait()
            self.state = "done"

        except RenderCancelled:
            self.state = "cancelled"
        except (CompileError, RenderFailed, BinaryMissing, ValueError) as exc:
            log.warning("Предпросмотр не собран: %s", exc)
            self.error = str(exc)
            self.state = "error"
        except Exception as exc:  # noqa: BLE001
            log.exception("Предпросмотр упал")
            self.error = f"{type(exc).__name__}: {exc}"
            self.state = "error"
        finally:
            self.finished_at = datetime.now(timezone.utc)
            if self.state in ("error", "cancelled") and target is not None and target.exists():
                # ffmpeg создаёт файл сразу через -y — при обрыве/ошибке он остаётся
                # пустым/битым на диске и на следующий запрос выдал бы себя за готовый
                # кеш (см. проверку `target.exists()` выше) — плеер получил бы такой файл
                # и упал бы на Range-запросе (416).
                target.unlink(missing_ok=True)
            _cleanup()

    def _on_progress(self, done: float, total: float) -> None:
        self.done_seconds, self.total_seconds = done, total

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "project_id": self.project_id,
            "done_seconds": round(self.done_seconds, 2),
            "total_seconds": round(self.total_seconds, 2),
            "output": self.output,
            "cached": self.cached,
            "error": self.error,
        }


preview_job = PreviewJob()


def drop_proxies(project_id: int) -> int:
    """Удаляет прокси-файлы проекта — после удаления проекта они уже не нужны."""
    removed = 0
    for stale in proxy_dir().glob(f"project{project_id}_*.mp4"):
        stale.unlink(missing_ok=True)
        removed += 1
    return removed
