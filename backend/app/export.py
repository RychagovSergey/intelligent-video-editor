"""Экспорт проекта в итоговый файл (ТЗ пп. 3.6, 5)."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import session_scope
from .ffmpeg.compile import CompileError, build_command, export_preset
from .ffmpeg.render import RenderCancelled, RenderFailed, render
from .ffmpeg.runner import BinaryMissing
from .timeline import store

log = logging.getLogger(__name__)

EXTENSIONS = {"mp4": ".mp4", "mov": ".mov", "webm": ".webm"}

#: Грубая оценка веса результата — нужна, чтобы не начинать экспорт на переполненный диск.
APPROX_BYTES_PER_SECOND = {"mp4": 1_500_000, "webm": 1_200_000, "mov": 25_000_000}
SPACE_SAFETY_FACTOR = 1.3


class ExportError(RuntimeError):
    pass


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "export"
    return cleaned[:120]


def check_space(directory: Path, container: str, duration: float) -> None:
    """Проверяет свободное место заранее: обрыв на середине рендера хуже отказа (ТЗ п. 5)."""
    needed = APPROX_BYTES_PER_SECOND.get(container, 2_000_000) * max(duration, 1) * SPACE_SAFETY_FACTOR
    free = shutil.disk_usage(directory).free
    if free < needed:
        raise ExportError(
            f"Недостаточно места: нужно примерно {needed / 1e9:.1f} ГБ, свободно {free / 1e9:.1f} ГБ"
        )


class ExportJob:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._started_at: float = 0.0

        self.state: str = "idle"      # idle | running | done | error | cancelled
        self.project_id: int | None = None
        self.output: str | None = None
        self.done_seconds: float = 0.0
        self.total_seconds: float = 0.0
        self.eta_seconds: float | None = None
        self.size_bytes: int | None = None
        self.error: str | None = None
        self.finished_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, project_id: int, *, directory: Path, filename: str, container: str,
              quality: str, bitrate: str | None) -> bool:
        with self._lock:
            if self.running:
                return False
            self._cancel.clear()
            self._started_at = time.monotonic()
            self.state = "running"
            self.project_id = project_id
            self.output = None
            self.done_seconds = self.total_seconds = 0.0
            self.eta_seconds = None
            self.size_bytes = None
            self.error = None
            self.finished_at = None
            self._thread = threading.Thread(
                target=self._run,
                args=(project_id, directory, filename, container, quality, bitrate),
                daemon=True,
            )
            self._thread.start()
            return True

    def cancel(self) -> bool:
        if not self.running:
            return False
        self._cancel.set()
        return True

    def _run(self, project_id: int, directory: Path, filename: str, container: str,
             quality: str, bitrate: str | None) -> None:
        target = directory / f"{safe_filename(filename)}{EXTENSIONS.get(container, '.mp4')}"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with session_scope() as db:
                timeline = store.load(db, project_id)
                if timeline.duration <= 0:
                    raise ExportError("Таймлайн пуст — экспортировать нечего")
                check_space(directory, container, timeline.duration)

                preset = export_preset(
                    container=container, quality=quality,
                    width=timeline.width, height=timeline.height, fps=timeline.fps,
                    bitrate=bitrate,
                )
                command = build_command(db, timeline, target, preset)
                self.total_seconds = command.duration
                render(command, on_progress=self._on_progress, should_stop=self._cancel.is_set)

            self.output = str(target)
            self.size_bytes = target.stat().st_size if target.exists() else None
            self.state = "done"

        except RenderCancelled:
            target.unlink(missing_ok=True)   # недоделанный файл пользователю не нужен
            self.state = "cancelled"
        except (ExportError, CompileError, RenderFailed, BinaryMissing, ValueError, OSError) as exc:
            log.warning("Экспорт не выполнен: %s", exc)
            target.unlink(missing_ok=True)
            self.error = str(exc)
            self.state = "error"
        except Exception as exc:  # noqa: BLE001
            log.exception("Экспорт упал")
            target.unlink(missing_ok=True)
            self.error = f"{type(exc).__name__}: {exc}"
            self.state = "error"
        finally:
            self.finished_at = datetime.now(timezone.utc)

    def _on_progress(self, done: float, total: float) -> None:
        self.done_seconds, self.total_seconds = done, total
        elapsed = time.monotonic() - self._started_at
        if done > 0.5 and total > 0:
            self.eta_seconds = max(0.0, elapsed / done * (total - done))

    def snapshot(self) -> dict[str, Any]:
        percent = (self.done_seconds / self.total_seconds * 100) if self.total_seconds else 0.0
        return {
            "state": self.state,
            "project_id": self.project_id,
            "output": self.output,
            "percent": round(min(percent, 100.0), 1),
            "done_seconds": round(self.done_seconds, 2),
            "total_seconds": round(self.total_seconds, 2),
            "eta_seconds": round(self.eta_seconds, 1) if self.eta_seconds is not None else None,
            "size_bytes": self.size_bytes,
            "error": self.error,
        }


export_job = ExportJob()


def reveal(path: Path) -> None:
    """Показывает готовый файл в Finder (ТЗ п. 3.6: «предложить открыть файл»)."""
    if not path.exists():
        raise ExportError("Файл не найден")
    subprocess.Popen(["open", "-R", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
