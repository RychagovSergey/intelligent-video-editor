"""Точка входа backend (ТЗ п. 2.1)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import init_db
from .routers import agent, analysis, media, projects, storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Intelligent Video Editor", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "tauri://localhost"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(storage.router)
app.include_router(media.router)
app.include_router(analysis.router)
app.include_router(projects.router)
app.include_router(agent.router)


@app.get("/api/health")
def health() -> dict:
    """Готовность окружения — показывается в нижней панели UI."""
    return {
        "status": "ok",
        "ffmpeg": settings.binary("ffmpeg"),
        "ffprobe": settings.binary("ffprobe"),
        "ffplay": settings.binary("ffplay"),
        "vl_model": settings.vl_model,
        "vl_model_fast": settings.vl_model_fast,
        "agent_provider": settings.agent_provider,
        # Ключи в ответ не попадают — только факт их наличия (Р-10).
        "agent_configured": bool(
            settings.custom_api_key if settings.agent_provider == "custom" else settings.openai_api_key
        ),
    }
