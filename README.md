# Intelligent Video Editor

**English | [Русский](README_RU.md)**

A desktop app for rough-cut video editing: local semantic analysis of your media
library via Qwen2.5-VL, a timeline editor, ffmpeg-based preview and export, plus an
LLM agent that assembles a rough cut from a text prompt ("make a punchy one-minute
clip from the ocean shots, cut to this track").

Everything about your media's *content* stays **local**: frame descriptions, objects
and scenes with timestamps, music tempo and beat grid. The only thing that leaves your
machine is the text of a request to the editing agent — and only if you use the agent.

![UI: timeline, metadata panel and the editing agent](assets/screenshot.png)

## Features

- **Storage indexing** — recursive incremental scan, file characteristics via
  ffprobe, thumbnails, corrupted files flagged.
- **Media analysis via Qwen2.5-VL** (local, through Ollama) — description, objects,
  style, mood; for video, scenes with timestamps so you can pick the right moment
  inside a long clip. Queue with progress, cancellation, and a lighter/faster model
  option.
- **Audio rhythm markup** — tempo, beats and bars (`beat_times`, `downbeats`), silences
  and volume peaks. Computed once during analysis, so a track can be picked by tempo.
- **Timeline editor** — three tracks (video, audio, text), add/move/trim/split/speed/
  fade/mute, transitions (crossfade / dip to black), text overlays, undo/redo.
- **Preview** — 640×360 cached proxy render with a built-in player; optionally a
  separate ffplay window.
- **Export** — MP4/H.264 (CRF 18/23/28 or bitrate), MOV/ProRes, WebM/VP9; progress with
  time estimate, cancellation, free-space check.
- **Editing agent** — assembles a rough cut from a text prompt via an external LLM
  with function calling, built on the same timeline operations; semantic material
  search (description embeddings), action log in the UI.
- **MCP server** — the same editing tools exposed to an external agent (Claude Code,
  Claude Desktop, etc.).

## Requirements

| Component | Version | Why |
|---|---|---|
| Python | 3.12 | backend |
| Node.js | 20+ | frontend (Vite + React) |
| ffmpeg, ffprobe | any recent | analysis, preview, export — **required** |
| ffplay | — | preview in a separate window, optional |
| [Ollama](https://ollama.com) | — | local media analysis |

ffmpeg binaries are picked up from `PATH`, or from paths set in `.env`
(`FFMPEG_PATH`, `FFPROBE_PATH`, `FFPLAY_PATH`).

The editing agent and semantic search need access to an external LLM (an
OpenAI-compatible endpoint) — without keys the app still runs, those two features
just aren't available: search falls back to keyword matching, and the agent reports
that no provider is configured.

## Install

```bash
git clone <url> "Video AI" && cd "Video AI"

python3.12 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt

npm install --prefix frontend

ollama pull qwen2.5vl:7b
ollama pull qwen2.5vl:3b
```

## Configure

```bash
cp .env.example .env
```

`.env` is not committed. The minimum to get started is `MEDIA_ROOTS`; root folders
can also be added from the UI.

| Variable | Meaning |
|---|---|
| `MEDIA_ROOTS` | media library roots, comma-separated, `~` is expanded |
| `DATA_DIR` | cache folder; empty — `data` inside the first root |
| `VL_MODEL`, `VL_MODEL_FAST` | analysis models in Ollama (regular and fast) |
| `OLLAMA_BASE_URL` | Ollama address, defaults to `http://localhost:11434` |
| `FRAME_SAMPLE_SECONDS`, `MAX_FRAMES` | frame sampling step and cap per video |
| `MODEL_IMAGE_MAX_SIDE` | max side to downscale frames to before sending to the model (640) |
| `AGENT_PROVIDER` | `openai` (default) or `custom` (any OpenAI-compatible endpoint) |
| `OPENAI_API_KEY`, `OPENAI_CHAT_MODEL` | the `openai` provider |
| `OPENAI_EMBED_MODEL` | embedding model for semantic search — needed regardless of `AGENT_PROVIDER` |
| `CUSTOM_BASE_URL`, `CUSTOM_API_KEY`, `CUSTOM_CHAT_MODEL` | the `custom` provider |
| `MAX_STEPS_AGENT`, `MAX_TOKENS_AGENT` | step and token budget per agent run |
| `FFMPEG_PATH`, `FFPROBE_PATH`, `FFPLAY_PATH` | binary paths, if not on `PATH` |

The rest of the settings, with detailed comments, are in
[.env.example](.env.example).

## Run

Two processes: the backend and the frontend dev server.

```bash
.venv/bin/uvicorn backend.app.main:app --reload --port 8001
```

```bash
npm run dev --prefix frontend
```

- UI — http://localhost:5173
- API docs — http://localhost:8001/docs

The dev server proxies `/api` to port 8001; for a different port use
`BACKEND_PORT=8000 npm run dev --prefix frontend` (see
[frontend/vite.config.ts](frontend/vite.config.ts)).

## Usage

1. **Add folders** to the library from the UI (or set `MEDIA_ROOTS`) and wait for the
   scan: files are indexed first, then their characteristics are read via ffprobe.
2. **Analyze files** — the analysis button runs the selected files through
   Qwen2.5-VL and stores the result in `meta.json`. Re-analysis with overwrite is a
   separate button; you need it when the markup is stale (for example, after an
   update that added tempo and beat-grid markup for audio).
3. **Build the cut** — manually on the timeline, or by asking the agent in the agent
   panel.
4. **Preview the result** — the preview builds a proxy and plays it in the built-in
   player.
5. **Export** — pick a format and quality, wait for progress.

The rules the editing agent follows live in [EDITOR_AGENT.md](EDITOR_AGENT.md) and are
read on every run — you can edit the prompt without touching any code.

## Where things are stored

Everything derived lives next to your media library, in `<library root>/data`
(overridable with `DATA_DIR`):

| Path | Contents |
|---|---|
| `app.db` | file index, metadata, projects, settings |
| `meta/` | `meta.json` per file, mirroring the storage layout |
| `thumbs/` | thumbnails for the file list |
| `proxy/` | preview proxy files (last 8 kept) |

The folder can be deleted entirely — the app will rebuild it, re-analyzing files as
needed. Old-scheme sidecars (`<file>.meta.json` next to the media file) are still
read, but new ones aren't created: your library stays untouched.

## Connecting an external agent (MCP)

The app exposes an MCP server on top of the same editing tools — you can hand the
edit off to a third-party agent. Transport is stdio:

```bash
.venv/bin/python -m backend.mcp_server
```

For Claude Code, one command is enough:

```bash
claude mcp add intelligent-video-editor -- "$PWD/.venv/bin/python" -m backend.mcp_server
```

A ready client config snippet (e.g. for `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "intelligent-video-editor": {
      "command": "/path/to/project/.venv/bin/python",
      "args": ["-m", "backend.mcp_server"],
      "cwd": "/path/to/project"
    }
  }
}
```

The server works against the same database and storage as the app, so the agent's
edits show up in the UI immediately. The model is the client's choice — there's none
inside the server, so `.env` keys aren't needed for this.

Tools: project management (`list_projects`, `create_project`), sourcing material
(`search_media`, `list_media`, `get_media_details`), music (`get_audio_bpm`,
`get_audio_events`), and the full editing surface (`get_timeline`, `clear_timeline`,
`add_clip`, `trim_clip`, `split_clip`, `move_clip`, `delete_clip`, `set_speed`,
`set_fade`, `mute_clip`, `set_transition`, `clear_transition`, `add_text_clip`,
`set_text_properties`, `close_gaps`). Each one accepts `project_id` or
`project_name`; without either, the most recently edited project is used. Editing
rules are exposed as the MCP prompt `editing_rules`, sourced from the same
`EDITOR_AGENT.md`.

## Development

```bash
.venv/bin/python -m pytest backend/tests -q     # full suite
cd frontend && npx tsc --noEmit                 # frontend type check
cd frontend && npm run lint                     # oxlint
```

Tests that need a real ffmpeg get one: they run the actual binaries against
synthetic files generated via `lavfi`, rather than mocking ffmpeg out. Without
ffmpeg on `PATH`, those tests are skipped.

### Repository layout

```
backend/
  app/
    agent/        agent loop, tools, semantic search
    analysis/     analysis via Ollama, meta.json schemas
    ffmpeg/       ffmpeg wrappers: probe, frames, compile, bpm, audio events
    routers/      REST API
    timeline/     timeline model and clip operations
  mcp_server.py   MCP server on top of the same tools
  tests/
frontend/src/     React + TypeScript, SPA
EDITOR_AGENT.md   system prompt for the editing agent
```

Core architectural idea: **one layer of timeline operations, three consumers.**
`backend/app/timeline/ops.py` holds every timeline edit with its checks; the REST
API (manual editing from the UI), the agent's tools, and the MCP server all sit on
top of it — no duplicated logic.

## Status

Core functionality (indexing, ffmpeg, analysis, timeline editor, preview, export,
editing agent with an MCP server) is implemented and covered by tests. Packaging
into a native shell (Electron/Tauri) hasn't been done yet, so for now the app runs
as two console processes, as described above.

## License

[GPL-3.0](LICENSE).
