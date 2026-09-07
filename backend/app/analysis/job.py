"""Очередь анализа: один файл за раз, с прогрессом и отменой (ТЗ пп. 3.2, 3.7)."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..models import AnalysisStatus, MediaFile, MetaCache
from .analyzer import DEFAULT_MAX_FRAMES, analyze_file, write_sidecar
from .ollama_client import OllamaClient, OllamaError

log = logging.getLogger(__name__)


class AnalysisJob:
    """На 16 ГБ ОЗУ модель держится в памяти одна, поэтому файлы обрабатываются последовательно."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

        self.state: str = "idle"           # idle | running | done | error | cancelled
        self.model: str | None = None
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.current_file: str | None = None
        self.current_step: str | None = None
        self.done: int = 0
        self.total: int = 0
        self.analyzed: int = 0
        self.failed: int = 0
        self.skipped: int = 0
        self.errors: list[str] = []
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, file_ids: list[int], *, model: str, force: bool, **options: Any) -> bool:
        with self._lock:
            if self.running:
                return False
            self._cancel.clear()
            self.state = "running"
            self.model = model
            self.started_at = datetime.now(timezone.utc)
            self.finished_at = None
            self.current_file = self.current_step = None
            self.done = self.analyzed = self.failed = self.skipped = 0
            self.total = len(file_ids)
            self.errors = []
            self.error = None
            self._thread = threading.Thread(
                target=self._run, args=(file_ids, model, force, options), daemon=True
            )
            self._thread.start()
            return True

    def cancel(self) -> bool:
        if not self.running:
            return False
        self._cancel.set()
        return True

    def _run(self, file_ids: list[int], model: str, force: bool, options: dict) -> None:
        client = OllamaClient()
        try:
            model = client.resolve_model(model)
            self.model = model
        except OllamaError as exc:
            self.error = str(exc)
            self.state = "error"
            self.finished_at = datetime.now(timezone.utc)
            return

        try:
            for file_id in file_ids:
                if self._cancel.is_set():
                    self.state = "cancelled"
                    return
                self._analyze_one(file_id, client, model, force, options)
                self.done += 1
            self.state = "done"
        except Exception as exc:  # noqa: BLE001 — задача не должна ронять сервер
            log.exception("Анализ прерван ошибкой")
            self.error = f"{type(exc).__name__}: {exc}"
            self.state = "error"
        finally:
            self.current_file = self.current_step = None
            self.finished_at = datetime.now(timezone.utc)

    def _analyze_one(
        self, file_id: int, client: OllamaClient, model: str, force: bool, options: dict
    ) -> None:
        with session_scope() as db:
            file = db.get(MediaFile, file_id)
            if file is None or file.missing:
                self.skipped += 1
                return
            # Существующий meta.json не перезаписывается без явного запроса (ТЗ п. 5).
            if file.has_meta and not force and file.analysis_status == AnalysisStatus.analyzed:
                self.skipped += 1
                return

            path = file.path
            kind = file.type
            self.current_file = file.filename
            self.current_step = None

        from pathlib import Path

        meta = analyze_file(
            Path(path),
            kind,
            model=model,
            client=client,
            every_seconds=options.get("every_seconds"),
            every_nth_frame=options.get("every_nth_frame"),
            max_frames=options.get("max_frames") or DEFAULT_MAX_FRAMES,
            on_step=lambda _i, _t, label: setattr(self, "current_step", label),
        )

        failed = bool(meta.get("analysis_failed"))
        sidecar_path: str | None = None
        try:
            sidecar_path = str(write_sidecar(Path(path), meta))
        except OSError as exc:
            # Папка только на чтение — метаданные всё равно остаются в базе.
            failed = True
            meta = {**meta, "analysis_failed": True, "error": f"Не удалось записать meta.json: {exc}"}
            log.warning("meta.json не записан рядом с %s: %s", path, exc)

        with session_scope() as db:
            file = db.get(MediaFile, file_id)
            if file is None:
                return
            file.has_meta = sidecar_path is not None
            file.meta_path = sidecar_path
            file.analysis_status = AnalysisStatus.failed if failed else AnalysisStatus.analyzed

            cache = db.execute(
                select(MetaCache).where(MetaCache.file_id == file_id)
            ).scalar_one_or_none()
            raw = json.dumps(meta, ensure_ascii=False)
            source_mtime = None
            if sidecar_path:
                from pathlib import Path as _P
                try:
                    source_mtime = _P(sidecar_path).stat().st_mtime
                except OSError:
                    source_mtime = None
            if cache is None:
                db.add(MetaCache(file_id=file_id, meta_json=raw, model=meta.get("model"),
                                 source_mtime=source_mtime))
            else:
                cache.meta_json = raw
                cache.model = meta.get("model")
                cache.source_mtime = source_mtime

        if failed:
            self.failed += 1
            message = f"{self.current_file}: {meta.get('error', 'неизвестная ошибка')}"
            self.errors.append(message)
        else:
            self.analyzed += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "model": self.model,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "current_file": self.current_file,
            "current_step": self.current_step,
            "done": self.done,
            "total": self.total,
            "analyzed": self.analyzed,
            "failed": self.failed,
            "skipped": self.skipped,
            "errors": self.errors[-20:],
            "error": self.error,
        }


analysis_job = AnalysisJob()


def default_model(fast: bool = False) -> str:
    return settings.vl_model_fast if fast else settings.vl_model
