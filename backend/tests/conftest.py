from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app.config import settings  # noqa: E402
from backend.app.models import Base  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def isolated_cache(tmp_path_factory) -> None:
    """Тесты не должны писать метаданные и прокси в настоящее хранилище пользователя."""
    settings.data_dir_override = str(tmp_path_factory.mktemp("cache"))


@pytest.fixture
def db(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
