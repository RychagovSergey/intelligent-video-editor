"""Простейшая очередь фоновых задач: на этапе 1 нужен только скан хранилища.

Задел под этап 3 (анализ): состояние задачи публикуется одинаково,
чтобы UI использовал один и тот же способ отображения прогресса.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import session_scope
from .probing import probe_pending
from .scanner import scan_roots

log = logging.getLogger(__name__)


class ScanJob:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state: str = "idle"
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.current_root: str | None = None
        self.stats: dict[str, Any] | None = None
        self.error: str | None = None
        self.phase: str | None = None          # scanning | probing
        self.current_file: str | None = None
        self.done: int = 0
        self.total: int = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, roots: list[Path]) -> bool:
        with self._lock:
            if self.running:
                return False
            self.state = "running"
            self.started_at = datetime.now(timezone.utc)
            self.finished_at = None
            self.stats = None
            self.error = None
            self.current_root = str(roots[0]) if roots else None
            self.phase = "scanning"
            self.current_file = None
            self.done = 0
            self.total = 0
            self._thread = threading.Thread(target=self._run, args=(roots,), daemon=True)
            self._thread.start()
            return True

    def _run(self, roots: list[Path]) -> None:
        try:
            with session_scope() as db:
                stats = scan_roots(db, roots)
            self.stats = stats.as_dict()

            # Вторая фаза: технические характеристики новых файлов (этап 2).
            self.phase = "probing"
            self.current_root = None
            with session_scope() as db:
                probe_stats = probe_pending(db, progress=self._on_probe)
            self.stats = {**self.stats, **probe_stats}
            self.state = "done"
        except Exception as exc:  # noqa: BLE001 — состояние задачи важнее типа ошибки
            log.exception("Скан завершился ошибкой")
            self.error = f"{type(exc).__name__}: {exc}"
            self.state = "error"
        finally:
            self.current_root = None
            self.current_file = None
            self.phase = None
            self.finished_at = datetime.now(timezone.utc)

    def _on_probe(self, done: int, total: int, filename: str) -> None:
        self.done, self.total, self.current_file = done, total, filename

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "current_root": self.current_root,
            "phase": self.phase,
            "current_file": self.current_file,
            "done": self.done,
            "total": self.total,
            "stats": self.stats,
            "error": self.error,
        }


scan_job = ScanJob()
