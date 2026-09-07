"""Фоновое чтение технических характеристик проиндексированных файлов (ТЗ пп. 3.2, 5)."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .ffmpeg.probe import probe
from .models import AnalysisStatus, MediaFile

log = logging.getLogger(__name__)

ProgressCb = Callable[[int, int, str], None]


def pending_query(force: bool = False):
    """Файлы, для которых характеристик ещё нет (или файл изменился после чтения)."""
    stmt = select(MediaFile).where(MediaFile.missing.is_(False))
    if not force:
        stmt = stmt.where(or_(MediaFile.probed_at.is_(None), MediaFile.probe_error.isnot(None)))
    return stmt


def probe_pending(
    db: Session,
    *,
    force: bool = False,
    progress: ProgressCb | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    files = list(db.execute(pending_query(force)).scalars())
    total = len(files)
    done = ok = failed = 0

    for file in files:
        if should_stop and should_stop():
            break
        if progress:
            progress(done, total, file.filename)

        info = probe(Path(file.path))
        file.probed_at = datetime.now(timezone.utc)

        if info.ok:
            file.duration = info.duration
            file.width = info.width
            file.height = info.height
            file.fps = info.fps
            file.video_codec = info.video_codec
            file.audio_codec = info.audio_codec
            file.bitrate = info.bitrate
            file.has_audio = info.has_audio
            file.has_cover_art = info.has_cover_art
            file.captured_at = info.captured_at
            file.probe_error = None
            # Файл, ранее помеченный повреждённым, снова читается — возвращаем в очередь анализа.
            if file.analysis_status == AnalysisStatus.corrupted:
                file.analysis_status = AnalysisStatus.analyzed if file.has_meta else AnalysisStatus.pending
            ok += 1
        else:
            # ffprobe не читает файл — анализировать его бессмысленно (ТЗ п. 5).
            file.probe_error = info.error
            file.analysis_status = AnalysisStatus.corrupted
            failed += 1
            log.warning("Файл не читается: %s (%s)", file.path, info.error)

        done += 1

    db.flush()
    if progress:
        progress(done, total, "")
    return {"probed": done, "ok": ok, "corrupted": failed}
