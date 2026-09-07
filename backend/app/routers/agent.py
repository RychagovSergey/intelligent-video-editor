"""Запуск агента и его состояние (ТЗ пп. 3.5, 4.5)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..agent.client import AgentUnavailable, current_provider
from ..agent.runner import INSTRUCTIONS_FILE, agent_job
from ..agent.search import EmbeddingsUnavailable, reindex
from ..db import get_db
from ..models import Project

router = APIRouter(prefix="/api/agent", tags=["agent"])


class RunIn(BaseModel):
    project_id: int
    # Детальный творческий бриф — с описанием ритма монтажа, цветокоррекции, тем клипов
    # и т. п. — легко выходит за пару тысяч символов, поэтому лимит с запасом.
    prompt: str = Field(min_length=3, max_length=8000)


class AgentStatusOut(BaseModel):
    state: str
    project_id: int | None = None
    prompt: str | None = None
    model: str | None = None
    step: int = 0
    max_steps: int = 0
    log: list[dict] = []
    answer: str | None = None
    error: str | None = None


@router.get("/info")
def info() -> dict:
    """Настроен ли агент — показывается в нижней панели; ключи наружу не отдаются."""
    try:
        provider = current_provider()
        configured, model, name, error = True, provider.model, provider.name, None
    except AgentUnavailable as exc:
        configured, model, name, error = False, None, None, str(exc)
    return {
        "configured": configured,
        "provider": name,
        "model": model,
        "error": error,
        "instructions_file": str(INSTRUCTIONS_FILE),
        "instructions_found": INSTRUCTIONS_FILE.exists(),
    }


@router.post("/run", response_model=AgentStatusOut)
def run(payload: RunIn, db: Session = Depends(get_db)) -> AgentStatusOut:
    if db.get(Project, payload.project_id) is None:
        raise HTTPException(404, detail="Проект не найден")
    try:
        current_provider()
    except AgentUnavailable as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    if not agent_job.start(payload.project_id, payload.prompt.strip()):
        raise HTTPException(409, detail="Агент уже работает")
    return AgentStatusOut(**agent_job.snapshot())


@router.get("/status", response_model=AgentStatusOut)
def status() -> AgentStatusOut:
    return AgentStatusOut(**agent_job.snapshot())


@router.post("/cancel", response_model=AgentStatusOut)
def cancel() -> AgentStatusOut:
    agent_job.cancel()
    return AgentStatusOut(**agent_job.snapshot())


@router.post("/reindex")
def rebuild_index(force: bool = False, db: Session = Depends(get_db)) -> dict:
    """Пересчитать векторы описаний для семантического поиска (Р-12)."""
    try:
        return reindex(db, force=force)
    except EmbeddingsUnavailable as exc:
        raise HTTPException(400, detail=str(exc)) from exc
