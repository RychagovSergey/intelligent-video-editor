"""Проекты через API: сохранение, операции, отмена (ТЗ п. 3.3)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db import get_db
from backend.app.main import app
from backend.app.models import AnalysisStatus, Base, Folder, MediaFile, MediaType


@pytest.fixture
def client(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'api.db'}", future=True)
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    with Factory() as seed:
        folder = Folder(path=str(tmp_path), name="storage", is_root=True)
        seed.add(folder)
        seed.flush()
        seed.add_all([
            MediaFile(folder_id=folder.id, filename="a.mp4", path=str(tmp_path / "a.mp4"),
                      type=MediaType.video, size=1, modified=1.0, duration=10.0,
                      analysis_status=AnalysisStatus.pending),
            MediaFile(folder_id=folder.id, filename="p.png", path=str(tmp_path / "p.png"),
                      type=MediaType.image, size=1, modified=1.0,
                      analysis_status=AnalysisStatus.pending),
            MediaFile(folder_id=folder.id, filename="t.mp3", path=str(tmp_path / "t.mp3"),
                      type=MediaType.audio, size=1, modified=1.0, duration=120.0,
                      analysis_status=AnalysisStatus.pending),
        ])
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
        yield test_client
    app.dependency_overrides.clear()


def new_project(client: TestClient) -> int:
    response = client.post("/api/projects", json={"name": "Тест"})
    assert response.status_code == 200
    return response.json()["id"]


def test_project_starts_empty_with_default_tracks(client: TestClient) -> None:
    pid = new_project(client)
    data = client.get(f"/api/projects/{pid}/timeline").json()

    assert data["duration"] == 0.0
    assert [t["kind"] for t in data["timeline"]["tracks"]] == ["video", "audio", "text"]


def test_clip_is_added_and_persisted(client: TestClient) -> None:
    pid = new_project(client)
    client.post(f"/api/projects/{pid}/clips", json={"source_id": 1})

    data = client.get(f"/api/projects/{pid}/timeline").json()
    clips = data["timeline"]["tracks"][0]["clips"]

    assert len(clips) == 1
    assert clips[0]["name"] == "a.mp4"
    assert data["duration"] == 10.0


def test_audio_lands_on_audio_track(client: TestClient) -> None:
    pid = new_project(client)
    client.post(f"/api/projects/{pid}/clips", json={"source_id": 3})

    tracks = client.get(f"/api/projects/{pid}/timeline").json()["timeline"]["tracks"]

    assert tracks[0]["clips"] == []
    assert len(tracks[1]["clips"]) == 1


def test_operations_chain_and_undo_restores_previous_state(client: TestClient) -> None:
    pid = new_project(client)
    client.post(f"/api/projects/{pid}/clips", json={"source_id": 1})
    clip_id = client.get(f"/api/projects/{pid}/timeline").json()["timeline"]["tracks"][0]["clips"][0]["id"]

    split = client.post(f"/api/projects/{pid}/clips/{clip_id}/split", json={"at": 4.0}).json()
    assert len(split["timeline"]["tracks"][0]["clips"]) == 2
    assert split["can_undo"] is True

    undone = client.post(f"/api/projects/{pid}/undo").json()
    assert len(undone["timeline"]["tracks"][0]["clips"]) == 1
    assert undone["can_redo"] is True

    redone = client.post(f"/api/projects/{pid}/redo").json()
    assert len(redone["timeline"]["tracks"][0]["clips"]) == 2


def test_undo_on_fresh_project_is_rejected(client: TestClient) -> None:
    pid = new_project(client)
    assert client.post(f"/api/projects/{pid}/undo").status_code == 400


def test_move_conflict_returns_400_with_message(client: TestClient) -> None:
    pid = new_project(client)
    client.post(f"/api/projects/{pid}/clips", json={"source_id": 1})
    client.post(f"/api/projects/{pid}/clips", json={"source_id": 1})
    second = client.get(f"/api/projects/{pid}/timeline").json()["timeline"]["tracks"][0]["clips"][1]["id"]

    response = client.patch(f"/api/projects/{pid}/clips/{second}/move", json={"start": 5.0})

    assert response.status_code == 400
    assert "клип" in response.json()["detail"].lower()


def test_speed_out_of_range_is_rejected_by_schema(client: TestClient) -> None:
    pid = new_project(client)
    client.post(f"/api/projects/{pid}/clips", json={"source_id": 1})
    clip_id = client.get(f"/api/projects/{pid}/timeline").json()["timeline"]["tracks"][0]["clips"][0]["id"]

    assert client.patch(f"/api/projects/{pid}/clips/{clip_id}/speed", json={"speed": 10}).status_code == 422


def test_missing_media_returns_404(client: TestClient) -> None:
    pid = new_project(client)
    assert client.post(f"/api/projects/{pid}/clips", json={"source_id": 999}).status_code == 404


def test_whole_timeline_can_be_replaced(client: TestClient) -> None:
    """Этим путём агент выкладывает готовый монтаж целиком (ТЗ п. 4.5)."""
    pid = new_project(client)
    timeline = {
        "version": 1, "fps": 30.0, "width": 1920, "height": 1080,
        "tracks": [
            {"id": "video_1", "kind": "video", "name": "Видео", "muted": False, "clips": [
                {"id": "c1", "source_id": 1, "name": "a.mp4", "kind": "video",
                 "start": 0.0, "in_point": 1.0, "out_point": 4.0, "speed": 1.0},
            ]},
            {"id": "audio_1", "kind": "audio", "name": "Аудио", "muted": False, "clips": []},
        ],
    }

    response = client.put(f"/api/projects/{pid}/timeline", json=timeline)

    assert response.status_code == 200
    assert response.json()["duration"] == 3.0


def test_project_deletion(client: TestClient) -> None:
    pid = new_project(client)
    assert client.delete(f"/api/projects/{pid}").status_code == 200
    assert client.get(f"/api/projects/{pid}/timeline").status_code == 404


def test_analysis_selection_with_force_includes_already_analyzed(client: TestClient) -> None:
    """«Анализ выбранного» разбирает файл заново, «Анализ всего» — только без метаданных."""
    from backend.app.db import SessionLocal  # noqa: F401  (соединение подменено фикстурой)
    from backend.app.routers.analysis import AnalyzeIn, _select_files

    db = next(app.dependency_overrides[get_db]())
    analyzed = db.get(MediaFile, 1)
    analyzed.analysis_status = AnalysisStatus.analyzed
    analyzed.has_meta = True
    db.flush()

    forced = _select_files(db, AnalyzeIn(file_ids=[1], force=True))
    skipped = _select_files(db, AnalyzeIn(force=False))

    assert [f.id for f in forced] == [1]
    assert 1 not in [f.id for f in skipped]


def test_deleting_project_removes_its_proxies(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    """Прокси удалённого проекта не должны оставаться на диске."""
    from backend.app import paths

    cache = tmp_path / "cache"
    monkeypatch.setattr(paths.settings, "data_dir_override", str(cache))
    proxy_dir = paths.proxy_dir()
    proxy_dir.mkdir(parents=True)

    pid = new_project(client)
    (proxy_dir / f"project{pid}_abc.mp4").write_bytes(b"proxy")
    (proxy_dir / "project999_other.mp4").write_bytes(b"other project")

    response = client.delete(f"/api/projects/{pid}")

    assert response.status_code == 200
    assert response.json()["proxies_removed"] == 1
    assert not (proxy_dir / f"project{pid}_abc.mp4").exists()
    assert (proxy_dir / "project999_other.mp4").exists()   # чужие файлы не трогаем


def test_media_file_is_served_for_viewing(client: TestClient, tmp_path: Path) -> None:
    """Просмотр файла в приложении: исходник отдаётся как есть."""
    (tmp_path / "a.mp4").write_bytes(b"fake video bytes")

    response = client.get("/api/media/1/file")

    assert response.status_code == 200
    assert response.content == b"fake video bytes"


def test_missing_file_on_disk_is_reported(client: TestClient) -> None:
    assert client.get("/api/media/2/file").status_code == 404


def test_playable_flag_matches_format(client: TestClient) -> None:
    """Браузер играет mp4 сам, а heic и mkv — нет, для них нужен запасной путь."""
    assert client.get("/api/media/1/playable").json()["native"] is True

    db = next(app.dependency_overrides[get_db]())
    exotic = db.get(MediaFile, 2)
    exotic.path = exotic.path.replace(".png", ".heic")
    db.commit()      # запрос идёт в отдельной сессии — правку нужно зафиксировать

    body = client.get("/api/media/2/playable").json()
    assert body["kind"] == "image"
    assert body["native"] is False


def test_estimate_counts_only_selected_folder(client: TestClient) -> None:
    """«Анализ папки» считает файлы выбранной ветки, а не всего хранилища."""
    db = next(app.dependency_overrides[get_db]())
    root = db.execute(select(Folder)).scalars().first()
    nested = Folder(path=root.path + "/nested", name="nested", parent_id=root.id)
    db.add(nested)
    db.flush()
    db.add(MediaFile(folder_id=nested.id, filename="deep.mp4", path=root.path + "/nested/deep.mp4",
                     type=MediaType.video, size=1, modified=1.0,
                     analysis_status=AnalysisStatus.pending))
    db.commit()

    whole = client.get("/api/analyze/estimate").json()["pending_total"]
    branch = client.get(f"/api/analyze/estimate?folder_id={nested.id}&recursive=true").json()["pending_total"]

    assert whole == 4          # три файла из фикстуры плюс вложенный
    assert branch == 1


def test_columns_added_by_migration_are_backfilled(tmp_path: Path) -> None:
    """Колонка, добавленная в существующую базу, не должна оставлять NULL в старых строках."""
    from sqlalchemy import text

    from backend.app import db as db_module

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}", future=True)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE media_files (id INTEGER PRIMARY KEY, folder_id INTEGER, filename TEXT,"
            " path TEXT, type TEXT, size INTEGER, modified REAL, meta_path TEXT,"
            " analysis_status TEXT, indexed_at TEXT)"
        ))
        conn.execute(text(
            "INSERT INTO media_files (id, folder_id, filename, path, type, size, modified,"
            " analysis_status, indexed_at) VALUES (1, 1, 'a.mp4', '/a.mp4', 'video', 1, 1.0,"
            " 'pending', '2026-01-01')"
        ))

    original = db_module.engine
    try:
        db_module.engine = engine
        db_module._add_missing_columns()
        db_module._repair_nulls()
    finally:
        db_module.engine = original

    with engine.begin() as conn:
        row = conn.execute(text("SELECT has_audio, has_cover_art, missing FROM media_files")).one()
    assert row == (0, 0, 0)
