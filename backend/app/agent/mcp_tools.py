"""Инструменты проектов поверх общего слоя — нужны только внешним агентам (ТЗ п. 4.5).

Встроенный агент работает в уже открытом проекте, а клиент MCP должен сначала
выбрать или создать его, поэтому эти три инструмента живут отдельно.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Project
from ..timeline import store
from ..timeline.schema import empty_timeline


def list_projects(db: Session) -> dict:
    rows = db.execute(select(Project).order_by(Project.updated_at.desc())).scalars().all()
    projects = []
    for project in rows:
        try:
            duration = round(store.load(db, project.id).duration, 2)
        except ValueError:
            duration = 0.0
        projects.append({
            "project_id": project.id,
            "name": project.name,
            "duration": duration,
            "updated_at": project.updated_at.isoformat() if project.updated_at else None,
        })
    return {"projects": projects}


def create_project(db: Session, name: str) -> dict:
    project = Project(name=name.strip() or "Новый проект",
                      timeline_json=empty_timeline().model_dump_json())
    db.add(project)
    db.flush()
    store.forget(project.id)
    return {"project_id": project.id, "name": project.name}


def resolve_project(db: Session, project_id: int | None, name: str | None) -> Project:
    """Находит проект по id или названию — внешнему агенту удобнее по названию."""
    if project_id is not None:
        project = db.get(Project, project_id)
        if project is None:
            raise LookupError(f"Проекта с id={project_id} нет")
        return project

    if name:
        project = db.execute(
            select(Project).where(Project.name == name.strip())
        ).scalars().first()
        if project is None:
            raise LookupError(f"Проекта «{name}» нет")
        return project

    project = db.execute(select(Project).order_by(Project.updated_at.desc())).scalars().first()
    if project is None:
        raise LookupError("В приложении ещё нет ни одного проекта — создайте его")
    return project
