"""Запуск анализа и его состояние (ТЗ пп. 3.2, 3.7)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analysis.job import analysis_job, default_model
from ..analysis.ollama_client import OllamaClient, OllamaError
from ..db import get_db
from ..models import AnalysisStatus, Folder, MediaFile, MediaType

router = APIRouter(prefix="/api", tags=["analysis"])


class AnalyzeIn(BaseModel):
    file_ids: list[int] | None = None
    folder_id: int | None = None
    recursive: bool = True
    force: bool = False          # перезаписать существующий meta.json (ТЗ п. 3.1)
    fast: bool = False           # быстрая VL-модель вместо основной (Р-9)
    max_frames: int | None = Field(default=None, ge=1, le=40)
    every_seconds: float | None = Field(default=None, gt=0, le=600)
    every_nth_frame: int | None = Field(default=None, ge=1)


class AnalyzeStatusOut(BaseModel):
    state: str
    model: str | None = None
    current_file: str | None = None
    current_step: str | None = None
    done: int = 0
    total: int = 0
    analyzed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = []
    error: str | None = None


def _select_files(db: Session, payload: AnalyzeIn) -> list[MediaFile]:
    stmt = select(MediaFile).where(MediaFile.missing.is_(False))

    if payload.file_ids:
        stmt = stmt.where(MediaFile.id.in_(payload.file_ids))
    elif payload.folder_id is not None:
        folder = db.get(Folder, payload.folder_id)
        if folder is None:
            raise HTTPException(404, detail="Папка не найдена")
        stmt = stmt.where(
            MediaFile.path.startswith(folder.path + "/")
            if payload.recursive
            else MediaFile.folder_id == folder.id
        )

    # Повреждённые файлы анализировать бессмысленно (ТЗ п. 5).
    stmt = stmt.where(MediaFile.analysis_status != AnalysisStatus.corrupted)
    files = list(db.execute(stmt.order_by(MediaFile.type, MediaFile.filename)).scalars())

    if not payload.force:
        files = [f for f in files if f.analysis_status != AnalysisStatus.analyzed]
    return files


@router.post("/analyze", response_model=AnalyzeStatusOut)
def start_analysis(payload: AnalyzeIn, db: Session = Depends(get_db)) -> AnalyzeStatusOut:
    files = _select_files(db, payload)
    if not files:
        raise HTTPException(400, detail="Нечего анализировать: все выбранные файлы уже разобраны")

    model = default_model(payload.fast)
    started = analysis_job.start(
        [f.id for f in files],
        model=model,
        force=payload.force,
        max_frames=payload.max_frames,
        every_seconds=payload.every_seconds,
        every_nth_frame=payload.every_nth_frame,
    )
    if not started:
        raise HTTPException(409, detail="Анализ уже выполняется")
    return AnalyzeStatusOut(**analysis_job.snapshot())


@router.get("/analyze/status", response_model=AnalyzeStatusOut)
def analysis_status() -> AnalyzeStatusOut:
    return AnalyzeStatusOut(**analysis_job.snapshot())


@router.post("/analyze/cancel", response_model=AnalyzeStatusOut)
def cancel_analysis() -> AnalyzeStatusOut:
    analysis_job.cancel()
    return AnalyzeStatusOut(**analysis_job.snapshot())


@router.get("/analyze/models")
def list_models() -> dict:
    """Какие VL-модели реально установлены — чтобы UI не предлагал недоступное (Р-9)."""
    client = OllamaClient()
    try:
        installed = client.available_models()
    except OllamaError as exc:
        return {"available": False, "error": str(exc), "models": [], "quality": None, "fast": None}

    quality, fast = default_model(False), default_model(True)
    return {
        "available": True,
        "models": installed,
        "quality": quality,
        "fast": fast,
        "quality_installed": quality in installed,
        "fast_installed": fast in installed,
    }


@router.get("/analyze/estimate")
def estimate(
    folder_id: int | None = None, recursive: bool = True, db: Session = Depends(get_db)
) -> dict:
    """Сколько файлов ждёт анализа — для подписи на кнопке. Без папки — по всему хранилищу."""
    stmt = select(MediaFile.type, MediaFile.analysis_status).where(
        MediaFile.missing.is_(False),
        MediaFile.analysis_status.notin_([AnalysisStatus.analyzed, AnalysisStatus.corrupted]),
    )
    if folder_id is not None:
        folder = db.get(Folder, folder_id)
        if folder is None:
            raise HTTPException(404, detail="Папка не найдена")
        stmt = stmt.where(
            MediaFile.path.startswith(folder.path + "/")
            if recursive
            else MediaFile.folder_id == folder.id
        )
    rows = list(db.execute(stmt).all())
    return {
        "pending_total": len(rows),
        "pending_video": sum(1 for t, _ in rows if t == MediaType.video),
        "pending_image": sum(1 for t, _ in rows if t == MediaType.image),
        "pending_audio": sum(1 for t, _ in rows if t == MediaType.audio),
    }
