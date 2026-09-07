"""MCP-сервер: те же инструменты монтажа для внешних агентов (ТЗ п. 4.5).

Тест поднимает сервер отдельным процессом и разговаривает с ним настоящим клиентом
по stdio — так же, как это будет делать Claude Desktop или другой агент.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app import paths
from backend.app.config import PROJECT_ROOT, settings
from backend.app.models import AnalysisStatus, Base, Folder, MediaFile, MediaType

mcp = pytest.importorskip("mcp", reason="пакет mcp не установлен")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


@pytest.fixture
def storage(tmp_path: Path):
    """Отдельные хранилище и база: сервер поднимается над ними, а не над реальными."""
    media = tmp_path / "media"
    media.mkdir()
    cache = tmp_path / "cache"

    ffmpeg = settings.binary("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg не установлен")
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=size=320x240:rate=25:duration=5", "-pix_fmt", "yuv420p",
         str(media / "clip.mp4")],
        check=True,
    )

    engine = create_engine(f"sqlite:///{cache / 'app.db'}", future=True)
    cache.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        folder = Folder(path=str(media), name="media", is_root=True)
        db.add(folder)
        db.flush()
        db.add(MediaFile(
            folder_id=folder.id, filename="clip.mp4", path=str(media / "clip.mp4"),
            type=MediaType.video, size=(media / "clip.mp4").stat().st_size,
            modified=(media / "clip.mp4").stat().st_mtime, duration=5.0, width=320, height=240,
            fps=25.0, has_audio=False, analysis_status=AnalysisStatus.pending,
        ))
        db.commit()

    return {"media": media, "cache": cache}


async def talk(storage: dict, script) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "backend.mcp_server"],
        cwd=str(PROJECT_ROOT),
        env={
            **os.environ,
            "DATA_DIR": str(storage["cache"]),
            "MEDIA_ROOTS": str(storage["media"]),
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await script(session)


def call(session, name: str, arguments: dict):
    async def run():
        result = await session.call_tool(name, arguments)
        return json.loads(result.content[0].text)

    return run()


def test_server_exposes_editing_tools(storage: dict) -> None:
    seen: dict = {}

    async def script(session) -> None:
        tools = (await session.list_tools()).tools
        seen["names"] = {t.name for t in tools}
        seen["add_clip_schema"] = next(t for t in tools if t.name == "add_clip").input_schema

    asyncio.run(talk(storage, script))

    assert {"list_projects", "create_project", "search_media", "add_clip", "get_timeline"} <= seen["names"]
    # Внешнему агенту нужен способ указать проект — его нет у встроенного.
    assert "project_id" in seen["add_clip_schema"]["properties"]
    assert "source_id" in seen["add_clip_schema"]["properties"]


def test_external_agent_can_build_a_montage(storage: dict) -> None:
    """Полный путь: создать проект, увидеть файлы, положить клип, прочитать таймлайн."""
    seen: dict = {}

    async def script(session) -> None:
        seen["project"] = await call(session, "create_project", {"name": "Из MCP"})
        seen["files"] = await call(session, "list_media", {"project_id": seen["project"]["project_id"]})
        source_id = seen["files"]["files"][0]["source_id"]
        seen["added"] = await call(session, "add_clip", {
            "project_id": seen["project"]["project_id"], "source_id": source_id, "duration": 3,
        })
        seen["timeline"] = await call(session, "get_timeline", {
            "project_id": seen["project"]["project_id"],
        })

    asyncio.run(talk(storage, script))

    assert seen["files"]["files"][0]["filename"] == "clip.mp4"
    assert seen["files"]["files"][0]["resolution"] == "320x240"
    assert seen["added"]["clip"]["duration"] == 3.0
    assert len(seen["timeline"]["tracks"][0]["clips"]) == 1


def test_changes_are_saved_between_calls(storage: dict) -> None:
    """Каждый вызов — своя транзакция: клип, добавленный одним вызовом, виден следующему."""
    seen: dict = {}

    async def script(session) -> None:
        project = await call(session, "create_project", {"name": "Сохранение"})
        files = await call(session, "list_media", {"project_id": project["project_id"]})
        await call(session, "add_clip", {
            "project_id": project["project_id"],
            "source_id": files["files"][0]["source_id"], "duration": 2,
        })
        seen["projects"] = await call(session, "list_projects", {})

    asyncio.run(talk(storage, script))

    saved = next(p for p in seen["projects"]["projects"] if p["name"] == "Сохранение")
    assert saved["duration"] == 2.0


def test_unknown_project_is_reported_not_crashed(storage: dict) -> None:
    seen: dict = {}

    async def script(session) -> None:
        seen["result"] = await call(session, "get_timeline", {"project_id": 999})

    asyncio.run(talk(storage, script))

    assert "999" in seen["result"]["error"]


def test_editing_rules_are_published_as_prompt(storage: dict) -> None:
    """Внешний агент забирает правила монтажа из того же EDITOR_AGENT.md."""
    seen: dict = {}

    async def script(session) -> None:
        seen["prompts"] = [p.name for p in (await session.list_prompts()).prompts]
        result = await session.get_prompt("editing_rules", {})
        seen["text"] = result.messages[0].content.text

    asyncio.run(talk(storage, script))

    assert seen["prompts"] == ["editing_rules"]
    assert "Правила монтажа" in seen["text"]
