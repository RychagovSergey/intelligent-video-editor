"""Чтение и запись таймлайна проекта + история для отмены (ТЗ п. 3.3)."""
from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models import Project
from .schema import Timeline, empty_timeline

log = logging.getLogger(__name__)

HISTORY_LIMIT = 50

#: История правок живёт в памяти процесса: после перезапуска проект открывается
#: в последнем сохранённом состоянии, но стопка отмен начинается заново.
_undo: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
_redo: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
_lock = threading.Lock()


class ProjectNotFound(LookupError):
    pass


def get_project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise ProjectNotFound(f"Проект {project_id} не найден")
    return project


def load(db: Session, project_id: int) -> Timeline:
    project = get_project(db, project_id)
    raw = project.timeline_json or ""
    if not raw.strip() or raw.strip() == "{}":
        return empty_timeline()
    try:
        return Timeline(**json.loads(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        log.error("Повреждён таймлайн проекта %s: %s", project_id, exc)
        raise ValueError(f"Не удалось прочитать таймлайн проекта: {exc}") from exc


def save(db: Session, project_id: int, timeline: Timeline, *, remember: bool = True) -> Timeline:
    """Сохраняет таймлайн; `remember` кладёт прежнее состояние в стопку отмен."""
    project = get_project(db, project_id)
    if remember:
        with _lock:
            _undo[project_id].append(project.timeline_json or "")
            _redo[project_id].clear()
    project.timeline_json = timeline.model_dump_json()
    project.updated_at = datetime.now(timezone.utc)
    db.flush()
    return timeline


def undo(db: Session, project_id: int) -> Timeline:
    project = get_project(db, project_id)
    with _lock:
        history = _undo[project_id]
        if not history:
            raise LookupError("Отменять нечего")
        previous = history.pop()
        _redo[project_id].append(project.timeline_json or "")
    project.timeline_json = previous
    db.flush()
    return load(db, project_id)


def redo(db: Session, project_id: int) -> Timeline:
    project = get_project(db, project_id)
    with _lock:
        history = _redo[project_id]
        if not history:
            raise LookupError("Повторять нечего")
        following = history.pop()
        _undo[project_id].append(project.timeline_json or "")
    project.timeline_json = following
    db.flush()
    return load(db, project_id)


def history_state(project_id: int) -> dict:
    return {"can_undo": bool(_undo[project_id]), "can_redo": bool(_redo[project_id])}


def forget(project_id: int) -> None:
    with _lock:
        _undo.pop(project_id, None)
        _redo.pop(project_id, None)
