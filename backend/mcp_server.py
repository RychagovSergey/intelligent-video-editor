"""MCP-сервер приложения: те же инструменты монтажа для внешних агентов (ТЗ п. 4.5).

Запуск клиентом (Claude Desktop, Claude Code и т. п.) по stdio:

    /путь/к/проекту/.venv/bin/python -m backend.mcp_server

Сервер работает с той же базой и тем же хранилищем, что и приложение, поэтому
изменения сразу видны в интерфейсе. Модель выбирает клиент — здесь её нет.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .app.agent import mcp_tools
from .app.agent.runner import instructions
from .app.agent.tools import TOOL_SPECS, Context, execute
from .app.db import init_db, session_scope
from .app.timeline import store

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("ive.mcp")

SERVER_NAME = "intelligent-video-editor"

#: Инструменты выбора проекта: встроенному агенту они не нужны, внешнему — обязательны.
PROJECT_TOOLS = [
    {
        "name": "list_projects",
        "description": "Список проектов монтажа с их длительностью.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "create_project",
        "description": "Создать пустой проект монтажа и вернуть его id.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "название проекта"}},
            "required": ["name"],
        },
    },
]

#: Все инструменты монтажа принимают проект: id или название.
PROJECT_ARGS = {
    "project_id": {"type": "integer", "description": "id проекта; без него — последний правленный"},
    "project_name": {"type": "string", "description": "название проекта вместо id"},
}


def _tool_list() -> list[types.Tool]:
    tools = [
        types.Tool(
            name=spec["name"],
            description=spec["description"],
            input_schema=spec["parameters"],
        )
        for spec in PROJECT_TOOLS
    ]
    for spec in TOOL_SPECS:
        function = spec["function"]
        schema = json.loads(json.dumps(function["parameters"]))  # копия, чтобы не портить исходник
        schema.setdefault("properties", {}).update(PROJECT_ARGS)
        tools.append(
            types.Tool(name=function["name"], description=function["description"], input_schema=schema)
        )
    return tools


def _call(name: str, arguments: dict[str, Any]) -> dict:
    """Исполняет инструмент в отдельной транзакции: клиент может звать их вразнобой."""
    with session_scope() as db:
        if name == "list_projects":
            return mcp_tools.list_projects(db)
        if name == "create_project":
            return mcp_tools.create_project(db, str(arguments.get("name") or ""))

        try:
            project = mcp_tools.resolve_project(
                db, arguments.get("project_id"), arguments.get("project_name")
            )
        except LookupError as exc:
            return {"error": str(exc)}

        timeline = store.load(db, project.id)
        ctx = Context(db=db, project_id=project.id, timeline=timeline)
        payload = {k: v for k, v in arguments.items() if k not in ("project_id", "project_name")}

        result = execute(ctx, name, payload)
        ctx.save()
        return {"project_id": project.id, "project_name": project.name, **result}


PROMPT_NAME = "editing_rules"


def build_server() -> Server:
    """Собирает сервер поверх низкоуровневого API: схемы инструментов берутся из TOOL_SPECS."""
    server: Server = Server(SERVER_NAME)

    async def list_tools(_ctx: Any, _params: types.PaginatedRequestParams) -> types.ListToolsResult:
        return types.ListToolsResult(tools=_tool_list())

    async def call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        # Работа с базой блокирующая — уводим её с событийного цикла.
        result = await asyncio.to_thread(_call, params.name, dict(params.arguments or {}))
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
            is_error="error" in result,
        )

    async def list_prompts(_ctx: Any, _params: types.PaginatedRequestParams) -> types.ListPromptsResult:
        return types.ListPromptsResult(prompts=[
            types.Prompt(
                name=PROMPT_NAME,
                description="Правила монтажа и порядок работы из EDITOR_AGENT.md этого проекта.",
            )
        ])

    async def get_prompt(_ctx: Any, params: types.GetPromptRequestParams) -> types.GetPromptResult:
        if params.name != PROMPT_NAME:
            raise ValueError(f"Неизвестный промпт: {params.name}")
        return types.GetPromptResult(
            description="Инструкции монтажёра",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(type="text", text=instructions()),
                )
            ],
        )

    server.add_request_handler("tools/list", types.PaginatedRequestParams, list_tools)
    server.add_request_handler("tools/call", types.CallToolRequestParams, call_tool)
    server.add_request_handler("prompts/list", types.PaginatedRequestParams, list_prompts)
    server.add_request_handler("prompts/get", types.GetPromptRequestParams, get_prompt)
    return server


async def main() -> None:
    init_db()
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
