from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, select

from backend.app.models import AnalysisStatus, MediaFile, MediaType
from backend.app.scanner import scan_roots


def make_tree(root: Path) -> None:
    (root / "clips").mkdir(parents=True)
    (root / "music").mkdir()
    (root / ".hidden").mkdir()
    (root / "Photos.photoslibrary").mkdir()
    (root / "clips" / "a.mp4").write_bytes(b"fake video")
    (root / "clips" / "b.MOV").write_bytes(b"fake video")
    (root / "clips" / "note.txt").write_text("не медиа")
    (root / "photo.jpg").write_bytes(b"fake image")
    (root / "music" / "track.mp3").write_bytes(b"fake audio")
    (root / ".hidden" / "skipped.mp4").write_bytes(b"skip")
    (root / "Photos.photoslibrary" / "inside.mp4").write_bytes(b"skip")


def test_scan_indexes_media_and_skips_the_rest(db, tmp_path: Path) -> None:
    root = tmp_path / "storage"
    make_tree(root)

    stats = scan_roots(db, [root])

    paths = {Path(f.path).name for f in db.execute(select(MediaFile)).scalars()}
    assert paths == {"a.mp4", "b.MOV", "photo.jpg", "track.mp3"}
    assert stats.files_added == 4
    assert stats.files_total == 4

    kinds = {f.filename: f.type for f in db.execute(select(MediaFile)).scalars()}
    assert kinds["a.mp4"] == MediaType.video
    assert kinds["photo.jpg"] == MediaType.image
    assert kinds["track.mp3"] == MediaType.audio


def test_files_without_sidecar_are_pending(db, tmp_path: Path) -> None:
    root = tmp_path / "storage"
    make_tree(root)
    scan_roots(db, [root])

    file = db.execute(select(MediaFile).where(MediaFile.filename == "photo.jpg")).scalar_one()
    assert file.has_meta is False
    assert file.analysis_status == AnalysisStatus.pending


def test_existing_sidecar_is_loaded_into_cache(db, tmp_path: Path) -> None:
    root = tmp_path / "storage"
    make_tree(root)
    meta = {"description": "закат над морем", "objects": ["море"], "model": "qwen2.5vl:7b"}
    (root / "photo.jpg.meta.json").write_text(json.dumps(meta), encoding="utf-8")

    stats = scan_roots(db, [root])

    file = db.execute(select(MediaFile).where(MediaFile.filename == "photo.jpg")).scalar_one()
    assert file.has_meta is True
    assert file.analysis_status == AnalysisStatus.analyzed
    assert file.meta is not None
    assert json.loads(file.meta.meta_json)["description"] == "закат над морем"
    assert file.meta.model == "qwen2.5vl:7b"
    assert stats.meta_loaded == 1


def test_failed_sidecar_marks_file_failed(db, tmp_path: Path) -> None:
    root = tmp_path / "storage"
    make_tree(root)
    (root / "photo.jpg.meta.json").write_text(
        json.dumps({"analysis_failed": True, "error": "модель недоступна"}), encoding="utf-8"
    )

    scan_roots(db, [root])

    file = db.execute(select(MediaFile).where(MediaFile.filename == "photo.jpg")).scalar_one()
    assert file.analysis_status == AnalysisStatus.failed


def test_rescan_is_incremental_and_does_not_duplicate(db, tmp_path: Path) -> None:
    root = tmp_path / "storage"
    make_tree(root)
    scan_roots(db, [root])

    stats = scan_roots(db, [root])

    assert stats.files_added == 0
    assert stats.files_updated == 0
    assert stats.files_total == 4
    assert db.execute(select(MediaFile)).scalars().all().__len__() == 4


def test_changed_file_marks_analysis_stale(db, tmp_path: Path) -> None:
    root = tmp_path / "storage"
    make_tree(root)
    (root / "photo.jpg.meta.json").write_text(json.dumps({"description": "x"}), encoding="utf-8")
    scan_roots(db, [root])

    photo = root / "photo.jpg"
    photo.write_bytes(b"fake image, modified")
    import os
    os.utime(photo, (photo.stat().st_atime + 10, photo.stat().st_mtime + 10))

    stats = scan_roots(db, [root])

    file = db.execute(select(MediaFile).where(MediaFile.filename == "photo.jpg")).scalar_one()
    assert stats.files_updated == 1
    assert file.analysis_status == AnalysisStatus.stale


def test_deleted_file_is_marked_missing_not_removed(db, tmp_path: Path) -> None:
    root = tmp_path / "storage"
    make_tree(root)
    scan_roots(db, [root])

    (root / "clips" / "a.mp4").unlink()
    stats = scan_roots(db, [root])

    file = db.execute(select(MediaFile).where(MediaFile.filename == "a.mp4")).scalar_one()
    assert file.missing is True
    assert stats.files_missing == 1


def test_missing_root_is_reported_not_raised(db, tmp_path: Path) -> None:
    stats = scan_roots(db, [tmp_path / "нет-такой-папки"])
    assert stats.errors and "не найдена" in stats.errors[0].lower()
    assert stats.files_total == 0


def test_orphan_index_is_dropped_when_root_removed(db, tmp_path: Path) -> None:
    """Смена корня хранилища убирает из индекса файлы прежнего корня."""
    from backend.app.models import Folder
    from backend.app.routers.storage import _drop_orphan_index

    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    make_tree(old_root)
    (new_root / "clips").mkdir(parents=True)
    (new_root / "clips" / "c.mp4").write_bytes(b"fake video")

    scan_roots(db, [old_root, new_root])
    assert db.execute(select(func.count(MediaFile.id))).scalar_one() == 5

    _drop_orphan_index(db, [new_root])
    db.flush()

    remaining = {f.filename for f in db.execute(select(MediaFile)).scalars()}
    assert remaining == {"c.mp4"}
    assert all(Path(f.path).is_relative_to(new_root) for f in db.execute(select(Folder)).scalars())


def test_cache_folder_inside_storage_is_not_indexed(db, tmp_path: Path, monkeypatch) -> None:
    """Прокси и миниатюры лежат внутри хранилища и не должны попадать в список медиа."""
    from backend.app import paths

    root = tmp_path / "storage"
    make_tree(root)
    monkeypatch.setattr(paths.settings, "data_dir_override", str(root / "data"))
    proxy = paths.proxy_dir()
    proxy.mkdir(parents=True)
    (proxy / "project1_abc.mp4").write_bytes(b"proxy")

    scan_roots(db, [root])

    names = {f.filename for f in db.execute(select(MediaFile)).scalars()}
    assert "project1_abc.mp4" not in names
    assert names == {"a.mp4", "b.MOV", "photo.jpg", "track.mp3"}


def test_metadata_is_read_from_cache_folder(db, tmp_path: Path, monkeypatch) -> None:
    from backend.app import paths

    root = tmp_path / "storage"
    make_tree(root)
    monkeypatch.setattr(paths.settings, "data_dir_override", str(tmp_path / "cache"))
    monkeypatch.setattr(paths.settings, "media_roots", str(root))

    target = paths.meta_path_for(root / "photo.jpg")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"description": "из кеша"}), encoding="utf-8")

    scan_roots(db, [root])

    file = db.execute(select(MediaFile).where(MediaFile.filename == "photo.jpg")).scalar_one()
    assert file.analysis_status == AnalysisStatus.analyzed
    assert json.loads(file.meta.meta_json)["description"] == "из кеша"


def test_legacy_sidecar_next_to_file_is_still_read(db, tmp_path: Path, monkeypatch) -> None:
    """Метаданные, созданные прежней схемой, не теряются после переезда кеша."""
    from backend.app import paths

    root = tmp_path / "storage"
    make_tree(root)
    monkeypatch.setattr(paths.settings, "data_dir_override", str(tmp_path / "cache"))
    (root / "photo.jpg.meta.json").write_text(json.dumps({"description": "старый сайдкар"}), encoding="utf-8")

    scan_roots(db, [root])

    file = db.execute(select(MediaFile).where(MediaFile.filename == "photo.jpg")).scalar_one()
    assert file.analysis_status == AnalysisStatus.analyzed
    assert json.loads(file.meta.meta_json)["description"] == "старый сайдкар"
