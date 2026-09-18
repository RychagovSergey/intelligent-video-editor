"""Панель настроек: правка .env, перечитывание, проверка окружения, логи (этап 8)."""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app import settings_file
from backend.app.config import Settings, settings
from backend.app.db import get_db
from backend.app.logbuffer import buffer
from backend.app.main import app
from backend.app.models import AnalysisStatus, Base, Folder, MediaFile, MediaType, MetaCache
from backend.app.routers.settings import SECRET_MASK
from backend.app.settings_file import render_env, write_env


def test_render_replaces_in_place_and_keeps_comments() -> None:
    text = "# шапка\nMAX_FRAMES=5\n\n# комментарий\nVL_MODEL=old\n"
    out = render_env(text, {"VL_MODEL": "qwen3.5:4b-mlx", "MAX_FRAMES": "8"})
    assert out == "# шапка\nMAX_FRAMES=8\n\n# комментарий\nVL_MODEL=qwen3.5:4b-mlx\n"


def test_render_appends_missing_keys_and_quotes_spaces() -> None:
    out = render_env("A=1\n", {"DATA_DIR": "~/Library/Application Support/IVE", "B": "x#y"})
    assert out.startswith("A=1\n\n# ---- Добавлено из панели настроек ----\n")
    assert 'DATA_DIR="~/Library/Application Support/IVE"' in out
    assert 'B="x#y"' in out
    # Закомментированная строка с тем же ключом не считается за значение.
    assert render_env("# A=old\n", {"A": "new"}).count("A=new") == 1


def test_write_env_starts_from_example_and_sets_mode(tmp_path: Path, monkeypatch) -> None:
    example = tmp_path / ".env.example"
    example.write_text("# подсказка\nVL_MODEL=\n", encoding="utf-8")
    monkeypatch.setattr(settings_file, "EXAMPLE_FILE", example)
    target = tmp_path / ".env"

    write_env({"VL_MODEL": "m"}, target)

    assert target.read_text(encoding="utf-8") == "# подсказка\nVL_MODEL=m\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch) -> Path:
    """Панель пишет во временный .env, а `Settings()` читает его же."""
    path = tmp_path / ".env"
    path.write_text("MAX_FRAMES=5\nOPENAI_API_KEY=sk-secret\n", encoding="utf-8")
    monkeypatch.setattr(settings_file, "ENV_FILE", path)
    monkeypatch.setitem(Settings.model_config, "env_file", path)
    monkeypatch.setattr(settings, "max_frames", 5)
    monkeypatch.setattr(settings, "openai_api_key", "sk-secret")
    monkeypatch.delenv("MAX_FRAMES", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("FRAME_SAMPLE_SECONDS", raising=False)
    return path


@pytest.fixture
def client(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'api.db'}", future=True)
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with Factory() as seed:
        folder = Folder(path=str(tmp_path), name="storage", is_root=True)
        seed.add(folder)
        seed.flush()
        file = MediaFile(folder_id=folder.id, filename="a.mp4", path=str(tmp_path / "a.mp4"),
                         type=MediaType.video, size=1, modified=1.0, duration=10.0,
                         analysis_status=AnalysisStatus.analyzed, has_meta=True)
        seed.add(file)
        seed.flush()
        seed.add(MetaCache(file_id=file.id, model="m", meta_json=json.dumps({
            "type": "video", "duration": 10.0, "summary": "старое", "objects": ["a"],
            "scenes": [{"time": 0.0, "description": "кадр", "quality": "высокое"}],
        })))
        seed.commit()

    def override():
        db = Factory()
        try:
            yield db
            db.commit()
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as test_client:
        yield test_client, Factory
    app.dependency_overrides.clear()


def test_get_masks_secrets_and_lists_groups(client, env_file: Path) -> None:
    test_client, _ = client
    body = test_client.get("/api/settings").json()
    fields = {f["key"]: f for g in body["groups"] for f in g["fields"]}
    assert fields["OPENAI_API_KEY"]["value"] == SECRET_MASK
    assert fields["MAX_FRAMES"]["value"] == 5
    assert fields["DATA_DIR"]["restart"] is True
    assert "sk-secret" not in json.dumps(body)


def test_put_writes_env_reloads_and_skips_untouched_secret(client, env_file: Path) -> None:
    test_client, _ = client
    response = test_client.put("/api/settings", json={"values": {
        "MAX_FRAMES": 9, "FRAME_SAMPLE_SECONDS": "1,5", "OPENAI_API_KEY": SECRET_MASK,
    }})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["saved"] == ["FRAME_SAMPLE_SECONDS", "MAX_FRAMES"]
    assert body["restart_required"] is False

    text = env_file.read_text(encoding="utf-8")
    assert "MAX_FRAMES=9" in text
    assert "FRAME_SAMPLE_SECONDS=1.5" in text
    assert "OPENAI_API_KEY=sk-secret" in text     # маска не затёрла ключ
    # Перечитано без перезапуска.
    assert settings.max_frames == 9
    assert settings.frame_sample_seconds == 1.5


def test_put_rejects_bad_values(client, env_file: Path) -> None:
    test_client, _ = client
    assert test_client.put("/api/settings", json={"values": {"MAX_FRAMES": "-1"}}).status_code == 400
    assert test_client.put("/api/settings", json={"values": {"NOPE": "1"}}).status_code == 400
    assert test_client.put(
        "/api/settings", json={"values": {"FFMPEG_PATH": "/nonexistent/ffmpeg"}}
    ).status_code == 400
    assert "MAX_FRAMES=5" in env_file.read_text(encoding="utf-8")


def test_check_reports_binaries_and_agent(client, monkeypatch) -> None:
    test_client, _ = client
    monkeypatch.setattr(settings, "ffmpeg_path", "/nonexistent/ffmpeg")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "agent_provider", "openai")
    checks = {c["id"]: c for c in test_client.get("/api/settings/check").json()}
    assert checks["ffmpeg"]["level"] == "error"
    assert checks["ffmpeg"]["hint"]
    assert checks["agent"]["level"] == "warn"
    assert checks["ollama"]["level"] in ("ok", "error")


def test_logs_are_served_incrementally(client) -> None:
    test_client, _ = client
    # Под pytest корневой логгер стоит на WARNING; в приложении basicConfig ставит INFO.
    logging.getLogger("test.panel").setLevel(logging.INFO)
    logging.getLogger("test.panel").warning("первая")
    first = test_client.get("/api/logs").json()
    assert any(e["message"] == "первая" and e["level"] == "WARNING" for e in first["entries"])

    logging.getLogger("test.panel").info("вторая")
    tail = test_client.get(f"/api/logs?after={first['last_seq']}").json()
    assert [e["message"] for e in tail["entries"]] == ["вторая"]
    assert tail["last_seq"] == buffer.last_seq


def test_meta_edit_updates_cache_and_sidecar(client, tmp_path: Path) -> None:
    test_client, Factory = client
    with Factory() as db:
        file_id = db.execute(select(MediaFile.id)).scalar_one()

    response = test_client.put(f"/api/media/{file_id}/meta", json={
        "fields": {"summary": " новое ", "objects": "a, b ,"},
        "scenes": {"0": {"description": "другой кадр"}},
    })
    assert response.status_code == 200, response.text
    meta = response.json()["meta"]
    assert meta["summary"] == "новое"
    assert meta["objects"] == ["a", "b"]
    assert meta["scenes"][0] == {"time": 0.0, "description": "другой кадр", "quality": "высокое"}
    assert meta["edited_at"]

    with Factory() as db:
        file = db.get(MediaFile, file_id)
        cached = json.loads(db.execute(select(MetaCache)).scalar_one().meta_json)
    assert cached["summary"] == "новое"
    assert file.meta_path and Path(file.meta_path).exists()
    assert json.loads(Path(file.meta_path).read_text(encoding="utf-8"))["summary"] == "новое"

    # Технические поля и время сцены не правятся.
    assert test_client.put(f"/api/media/{file_id}/meta", json={"fields": {"duration": 1}}).status_code == 400
    assert test_client.put(
        f"/api/media/{file_id}/meta", json={"scenes": {"0": {"time": 3}}}
    ).status_code == 400
