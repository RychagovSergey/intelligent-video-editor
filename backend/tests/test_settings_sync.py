"""Согласование корней хранилища между .env и базой (Р-11)."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app import db as db_module
from backend.app.config import settings
from backend.app.db import (
    ENV_ROOTS_KEY, MEDIA_ROOTS_KEY, get_setting, set_setting, sync_roots_from_env,
)
from backend.app.models import Base


@pytest.fixture
def db(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'settings.db'}", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def use_env_roots(monkeypatch, value: str) -> None:
    monkeypatch.setattr(settings, "media_roots", value)


def test_first_start_takes_roots_from_env(db: Session, tmp_path: Path, monkeypatch) -> None:
    use_env_roots(monkeypatch, str(tmp_path / "library"))

    roots = sync_roots_from_env(db)

    assert roots == [str(tmp_path / "library")]
    assert get_setting(db, MEDIA_ROOTS_KEY) == roots


def test_ui_choice_survives_restart_when_env_unchanged(db: Session, tmp_path: Path, monkeypatch) -> None:
    use_env_roots(monkeypatch, str(tmp_path / "library"))
    sync_roots_from_env(db)

    # Пользователь выбрал другую папку в интерфейсе.
    set_setting(db, MEDIA_ROOTS_KEY, [str(tmp_path / "picked-in-ui")])

    assert sync_roots_from_env(db) == [str(tmp_path / "picked-in-ui")]


def test_changed_env_overrides_stored_roots(db: Session, tmp_path: Path, monkeypatch) -> None:
    """Правка MEDIA_ROOTS в .env должна давать эффект, иначе файл настроек бесполезен."""
    use_env_roots(monkeypatch, str(tmp_path / "old"))
    sync_roots_from_env(db)
    set_setting(db, MEDIA_ROOTS_KEY, [str(tmp_path / "picked-in-ui")])

    use_env_roots(monkeypatch, str(tmp_path / "new"))
    roots = sync_roots_from_env(db)

    assert roots == [str(tmp_path / "new")]
    assert get_setting(db, ENV_ROOTS_KEY) == roots


def test_empty_env_does_not_wipe_stored_roots(db: Session, tmp_path: Path, monkeypatch) -> None:
    use_env_roots(monkeypatch, str(tmp_path / "library"))
    sync_roots_from_env(db)

    use_env_roots(monkeypatch, "")

    assert sync_roots_from_env(db) == [str(tmp_path / "library")]


def test_legacy_database_is_not_copied_into_new_cache(tmp_path: Path, monkeypatch) -> None:
    """Старая база не должна сама переезжать в свежий корень вместе с чужими путями."""
    legacy_dir = tmp_path / "project" / "data"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "app.db").write_bytes(b"legacy database")

    monkeypatch.setattr(db_module, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.setattr(settings, "data_dir_override", str(tmp_path / "fresh-cache"))

    target = db_module._prepare_storage()

    assert target == tmp_path / "fresh-cache" / "app.db"
    assert not target.exists()
