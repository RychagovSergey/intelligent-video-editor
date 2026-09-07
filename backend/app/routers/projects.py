"""Проекты монтажа и операции над таймлайном (ТЗ пп. 3.3, 4.5)."""
from __future__ import annotations

from datetime import datetime

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import MediaFile, MediaType, Project, enum_value
from ..config import expand
from ..export import export_job, reveal
from ..preview import drop_proxies, preview_job, proxy_path
from ..timeline import ops, store
from ..timeline.schema import Timeline, empty_timeline

router = APIRouter(prefix="/api/projects", tags=["projects"])


# --------------------------------------------------------------------------- схемы


class ProjectOut(BaseModel):
    id: int
    name: str
    created_at: datetime
    updated_at: datetime
    duration: float = 0.0


class ProjectIn(BaseModel):
    name: str = "Новый проект"


class TimelineOut(BaseModel):
    project_id: int
    timeline: Timeline
    duration: float
    can_undo: bool = False
    can_redo: bool = False
    #: Прокси для ЭТОГО состояния таймлайна уже собран — превью не устарело.
    preview_ready: bool = False


class AddClipIn(BaseModel):
    source_id: int
    track_id: str | None = None
    start: float | None = None
    in_point: float = 0.0
    out_point: float | None = None
    duration: float | None = None      # для изображений — длительность показа
    speed: float = 1.0


class MoveClipIn(BaseModel):
    start: float
    track_id: str | None = None


class TrimClipIn(BaseModel):
    in_point: float | None = None
    out_point: float | None = None
    keep_start: bool = True


class SplitClipIn(BaseModel):
    at: float


class SpeedIn(BaseModel):
    speed: float = Field(ge=0.25, le=4.0)


class MuteIn(BaseModel):
    muted: bool


class FadeIn(BaseModel):
    fade_in: float | None = Field(default=None, ge=0)
    fade_out: float | None = Field(default=None, ge=0)


class TransitionIn(BaseModel):
    kind: str = "crossfade"
    duration: float = Field(default=0.5, gt=0)


class AddTextClipIn(BaseModel):
    text: str = Field(min_length=1, max_length=200)
    duration: float = Field(gt=0)
    start: float | None = None
    font_size: int = Field(default=48, ge=8, le=300)
    position: str = "bottom"
    animation: str = "fade"


class TextPropertiesIn(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=200)
    font_size: int | None = Field(default=None, ge=8, le=300)
    position: str | None = None
    animation: str | None = None


# --------------------------------------------------------------------------- вспомогательное


def _result(db: Session, project_id: int, timeline: Timeline) -> TimelineOut:
    return TimelineOut(
        project_id=project_id,
        timeline=timeline,
        duration=timeline.duration,
        preview_ready=proxy_path(project_id, timeline.model_dump_json()).exists(),
        **store.history_state(project_id),
    )


def _load(db: Session, project_id: int) -> Timeline:
    try:
        return store.load(db, project_id)
    except store.ProjectNotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


def _apply(db: Session, project_id: int, timeline: Timeline) -> TimelineOut:
    store.save(db, project_id, timeline)
    return _result(db, project_id, timeline)


def _guard(action) -> None:
    try:
        action()
    except ops.TimelineError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


# --------------------------------------------------------------------------- проекты


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)) -> list[ProjectOut]:
    projects = db.execute(select(Project).order_by(Project.updated_at.desc())).scalars().all()
    out = []
    for project in projects:
        try:
            duration = store.load(db, project.id).duration
        except ValueError:
            duration = 0.0
        out.append(
            ProjectOut(
                id=project.id, name=project.name, created_at=project.created_at,
                updated_at=project.updated_at, duration=duration,
            )
        )
    return out


@router.post("", response_model=ProjectOut)
def create_project(payload: ProjectIn, db: Session = Depends(get_db)) -> ProjectOut:
    project = Project(name=payload.name.strip() or "Новый проект",
                      timeline_json=empty_timeline().model_dump_json())
    db.add(project)
    db.flush()
    # У нового проекта истории нет — в том числе если его id повторно занят
    # после удаления прежнего проекта.
    store.forget(project.id)
    return ProjectOut(
        id=project.id, name=project.name, created_at=project.created_at,
        updated_at=project.updated_at, duration=0.0,
    )


@router.delete("/{project_id}")
def delete_project(project_id: int, db: Session = Depends(get_db)) -> dict:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, detail="Проект не найден")
    db.delete(project)
    store.forget(project_id)
    removed = drop_proxies(project_id)
    return {"deleted": project_id, "proxies_removed": removed}


@router.get("/{project_id}/timeline", response_model=TimelineOut)
def get_timeline(project_id: int, db: Session = Depends(get_db)) -> TimelineOut:
    return _result(db, project_id, _load(db, project_id))


@router.put("/{project_id}/timeline", response_model=TimelineOut)
def replace_timeline(project_id: int, timeline: Timeline, db: Session = Depends(get_db)) -> TimelineOut:
    """Замена целиком — этим пользуется агент, когда собирает монтаж сразу (ТЗ п. 4.5)."""
    _load(db, project_id)   # проверяем, что проект существует
    return _apply(db, project_id, timeline)


# --------------------------------------------------------------------------- клипы


@router.post("/{project_id}/clips", response_model=TimelineOut)
def add_clip(project_id: int, payload: AddClipIn, db: Session = Depends(get_db)) -> TimelineOut:
    timeline = _load(db, project_id)

    file = db.get(MediaFile, payload.source_id)
    if file is None or file.missing:
        raise HTTPException(404, detail="Медиафайл не найден")

    kind = enum_value(file.type)
    out_point = payload.out_point
    if out_point is None and payload.duration is not None:
        out_point = payload.in_point + payload.duration

    def action() -> None:
        ops.add_clip(
            timeline,
            source_id=file.id,
            kind=kind,
            name=file.filename,
            track_id=payload.track_id,
            start=payload.start,
            in_point=payload.in_point,
            out_point=out_point,
            source_duration=file.duration,
            speed=payload.speed,
        )

    _guard(action)
    return _apply(db, project_id, timeline)


@router.patch("/{project_id}/clips/{clip_id}/move", response_model=TimelineOut)
def move_clip(project_id: int, clip_id: str, payload: MoveClipIn, db: Session = Depends(get_db)) -> TimelineOut:
    timeline = _load(db, project_id)
    _guard(lambda: ops.move_clip(timeline, clip_id, start=payload.start, track_id=payload.track_id))
    return _apply(db, project_id, timeline)


@router.patch("/{project_id}/clips/{clip_id}/trim", response_model=TimelineOut)
def trim_clip(project_id: int, clip_id: str, payload: TrimClipIn, db: Session = Depends(get_db)) -> TimelineOut:
    timeline = _load(db, project_id)
    _, clip = timeline.find_clip(clip_id)
    if clip is None:
        raise HTTPException(404, detail="Клип не найден")
    file = db.get(MediaFile, clip.source_id)

    _guard(lambda: ops.trim_clip(
        timeline, clip_id,
        in_point=payload.in_point, out_point=payload.out_point,
        source_duration=file.duration if file else None,
        keep_start=payload.keep_start,
    ))
    return _apply(db, project_id, timeline)


@router.post("/{project_id}/clips/{clip_id}/split", response_model=TimelineOut)
def split_clip(project_id: int, clip_id: str, payload: SplitClipIn, db: Session = Depends(get_db)) -> TimelineOut:
    timeline = _load(db, project_id)
    _guard(lambda: ops.split_clip(timeline, clip_id, at=payload.at))
    return _apply(db, project_id, timeline)


@router.patch("/{project_id}/clips/{clip_id}/speed", response_model=TimelineOut)
def set_speed(project_id: int, clip_id: str, payload: SpeedIn, db: Session = Depends(get_db)) -> TimelineOut:
    timeline = _load(db, project_id)
    _guard(lambda: ops.set_speed(timeline, clip_id, speed=payload.speed))
    return _apply(db, project_id, timeline)


@router.patch("/{project_id}/clips/{clip_id}/mute", response_model=TimelineOut)
def mute_clip(project_id: int, clip_id: str, payload: MuteIn, db: Session = Depends(get_db)) -> TimelineOut:
    """Отключает собственный звук клипа — чтобы он не накладывался на музыку."""
    timeline = _load(db, project_id)
    _guard(lambda: ops.set_muted(timeline, clip_id, muted=payload.muted))
    return _apply(db, project_id, timeline)


@router.patch("/{project_id}/clips/{clip_id}/fade", response_model=TimelineOut)
def set_fade(project_id: int, clip_id: str, payload: FadeIn, db: Session = Depends(get_db)) -> TimelineOut:
    """Плавное появление/затухание клипа (видео и звук вместе)."""
    timeline = _load(db, project_id)
    _guard(lambda: ops.set_fade(timeline, clip_id, fade_in=payload.fade_in, fade_out=payload.fade_out))
    return _apply(db, project_id, timeline)


@router.patch("/{project_id}/clips/{clip_id}/transition", response_model=TimelineOut)
def set_transition(project_id: int, clip_id: str, payload: TransitionIn, db: Session = Depends(get_db)) -> TimelineOut:
    """Переход (кроссфейд, dip to black) между этим клипом и предыдущим на дорожке."""
    timeline = _load(db, project_id)
    _guard(lambda: ops.set_transition(timeline, clip_id, kind=payload.kind, duration=payload.duration))
    return _apply(db, project_id, timeline)


@router.delete("/{project_id}/clips/{clip_id}/transition", response_model=TimelineOut)
def clear_transition(project_id: int, clip_id: str, db: Session = Depends(get_db)) -> TimelineOut:
    timeline = _load(db, project_id)
    _guard(lambda: ops.clear_transition(timeline, clip_id))
    return _apply(db, project_id, timeline)


@router.post("/{project_id}/text_clips", response_model=TimelineOut)
def add_text_clip(project_id: int, payload: AddTextClipIn, db: Session = Depends(get_db)) -> TimelineOut:
    """Текстовый слой (заголовок/подпись) поверх видео — дорожка `text_1`."""
    timeline = _load(db, project_id)
    _guard(lambda: ops.add_text_clip(
        timeline, text=payload.text, duration=payload.duration, start=payload.start,
        font_size=payload.font_size, position=payload.position, animation=payload.animation,
    ))
    return _apply(db, project_id, timeline)


@router.patch("/{project_id}/clips/{clip_id}/text", response_model=TimelineOut)
def set_text_properties(project_id: int, clip_id: str, payload: TextPropertiesIn, db: Session = Depends(get_db)) -> TimelineOut:
    """Правит текст/оформление уже добавленного текстового клипа."""
    timeline = _load(db, project_id)
    _guard(lambda: ops.set_text_properties(
        timeline, clip_id, text=payload.text, font_size=payload.font_size,
        position=payload.position, animation=payload.animation,
    ))
    return _apply(db, project_id, timeline)


@router.delete("/{project_id}/clips/{clip_id}", response_model=TimelineOut)
def delete_clip(project_id: int, clip_id: str, db: Session = Depends(get_db)) -> TimelineOut:
    timeline = _load(db, project_id)
    _guard(lambda: ops.delete_clip(timeline, clip_id))
    return _apply(db, project_id, timeline)


@router.post("/{project_id}/tracks/{track_id}/close_gaps", response_model=TimelineOut)
def close_gaps(project_id: int, track_id: str, db: Session = Depends(get_db)) -> TimelineOut:
    timeline = _load(db, project_id)
    _guard(lambda: ops.close_gaps(timeline, track_id))
    return _apply(db, project_id, timeline)


# --------------------------------------------------------------------------- история


@router.post("/{project_id}/undo", response_model=TimelineOut)
def undo(project_id: int, db: Session = Depends(get_db)) -> TimelineOut:
    try:
        timeline = store.undo(db, project_id)
    except store.ProjectNotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return _result(db, project_id, timeline)


@router.post("/{project_id}/redo", response_model=TimelineOut)
def redo(project_id: int, db: Session = Depends(get_db)) -> TimelineOut:
    try:
        timeline = store.redo(db, project_id)
    except store.ProjectNotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return _result(db, project_id, timeline)


# --------------------------------------------------------------------------- предпросмотр


class PreviewOut(BaseModel):
    state: str
    project_id: int | None = None
    done_seconds: float = 0.0
    total_seconds: float = 0.0
    output: str | None = None
    cached: bool = False
    error: str | None = None


@router.post("/{project_id}/preview", response_model=PreviewOut)
def start_preview(
    project_id: int, player: str = "embedded", db: Session = Depends(get_db)
) -> PreviewOut:
    """Собирает прокси-файл: `player=embedded` — играть в приложении, `ffplay` — отдельным окном."""
    timeline = _load(db, project_id)
    if timeline.duration <= 0:
        raise HTTPException(400, detail="Таймлайн пуст — предпросматривать нечего")
    if not preview_job.start(project_id, launch_player=(player == "ffplay")):
        raise HTTPException(409, detail="Предпросмотр уже готовится")
    return PreviewOut(**preview_job.snapshot())


@router.get("/{project_id}/preview/file")
def preview_file(project_id: int, db: Session = Depends(get_db)) -> FileResponse:
    """Отдаёт готовый прокси для встроенного плеера (перемотка — через Range-запросы)."""
    timeline = _load(db, project_id)
    target = proxy_path(project_id, timeline.model_dump_json())
    if not target.exists():
        raise HTTPException(404, detail="Прокси ещё не собран")
    return FileResponse(
        Path(target), media_type="video/mp4", headers={"Cache-Control": "no-cache"}
    )


@router.get("/preview/status", response_model=PreviewOut)
def preview_status() -> PreviewOut:
    return PreviewOut(**preview_job.snapshot())


@router.post("/preview/cancel", response_model=PreviewOut)
def cancel_preview() -> PreviewOut:
    preview_job.cancel()
    return PreviewOut(**preview_job.snapshot())


# --------------------------------------------------------------------------- экспорт


class ExportIn(BaseModel):
    directory: str = "~/Movies"
    filename: str | None = None
    container: str = Field(default="mp4", pattern="^(mp4|mov|webm)$")
    quality: str = Field(default="medium", pattern="^(high|medium|low)$")
    bitrate: str | None = Field(default=None, pattern=r"^\d+(k|K|m|M)?$")


class ExportOut(BaseModel):
    state: str
    project_id: int | None = None
    output: str | None = None
    percent: float = 0.0
    done_seconds: float = 0.0
    total_seconds: float = 0.0
    eta_seconds: float | None = None
    size_bytes: int | None = None
    error: str | None = None


@router.post("/{project_id}/export", response_model=ExportOut)
def start_export(project_id: int, payload: ExportIn, db: Session = Depends(get_db)) -> ExportOut:
    timeline = _load(db, project_id)
    if timeline.duration <= 0:
        raise HTTPException(400, detail="Таймлайн пуст — экспортировать нечего")

    directory = expand(payload.directory)
    if directory.exists() and not directory.is_dir():
        raise HTTPException(400, detail=f"Это не папка: {directory}")

    project = db.get(Project, project_id)
    name = payload.filename or (project.name if project else f"project{project_id}")

    if not export_job.start(
        project_id,
        directory=directory,
        filename=name,
        container=payload.container,
        quality=payload.quality,
        bitrate=payload.bitrate,
    ):
        raise HTTPException(409, detail="Экспорт уже выполняется")
    return ExportOut(**export_job.snapshot())


@router.get("/export/status", response_model=ExportOut)
def export_status() -> ExportOut:
    return ExportOut(**export_job.snapshot())


@router.post("/export/cancel", response_model=ExportOut)
def cancel_export() -> ExportOut:
    export_job.cancel()
    return ExportOut(**export_job.snapshot())


@router.post("/export/reveal")
def reveal_export() -> dict:
    """Показать готовый файл в Finder."""
    if not export_job.output:
        raise HTTPException(400, detail="Экспортированного файла нет")
    try:
        reveal(Path(export_job.output))
    except RuntimeError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    return {"revealed": export_job.output}
