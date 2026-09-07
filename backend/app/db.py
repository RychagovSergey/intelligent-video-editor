"""Подключение к SQLite и сессии."""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from .config import PROJECT_ROOT
from .models import Base, Setting
from .paths import cache_dir, db_path

log = logging.getLogger(__name__)


def _prepare_storage() -> Path:
    """Готовит папку кеша под текущим корнем хранилища.

    База из прежнего расположения намеренно не переносится: перенос втаскивал
    в свежий корень чужой индекс вместе с сохранёнными в нём путями.
    """
    cache_dir().mkdir(parents=True, exist_ok=True)
    target = db_path()
    legacy = PROJECT_ROOT / "data" / "app.db"
    if not target.exists() and legacy.exists() and legacy != target:
        log.info(
            "Найдена база прежней версии: %s. Она не используется; при необходимости "
            "скопируйте её в %s вручную.", legacy, target,
        )
    return target


DB_FILE = _prepare_storage()

engine = create_engine(
    f"sqlite:///{DB_FILE}",
    future=True,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)

MEDIA_ROOTS_KEY = "media_roots"
#: Значение MEDIA_ROOTS, с которым приложение стартовало в прошлый раз.
ENV_ROOTS_KEY = "media_roots_from_env"


def init_db() -> None:
    """Создаёт таблицы, догоняет схему и при первом запуске заполняет корни из .env (Р-11)."""
    Base.metadata.create_all(engine)
    _add_missing_columns()
    _repair_nulls()
    with session_scope() as db:
        sync_roots_from_env(db)


def sync_roots_from_env(db: Session) -> list[str]:
    """Согласует корни хранилища с `.env`.

    Правки из интерфейса живут в базе и переживают перезапуск, но если
    `MEDIA_ROOTS` в `.env` изменился с прошлого запуска — побеждает `.env`.
    Иначе правка файла настроек не давала никакого эффекта.
    """
    from .config import settings

    env_roots = [str(p) for p in settings.default_media_roots]
    stored = get_setting(db, MEDIA_ROOTS_KEY, None)
    seen = get_setting(db, ENV_ROOTS_KEY, None)

    if not env_roots:
        # Пустой MEDIA_ROOTS не должен стирать папки, выбранные в интерфейсе.
        return stored or []

    if stored is None or seen != env_roots:
        set_setting(db, MEDIA_ROOTS_KEY, env_roots)
        set_setting(db, ENV_ROOTS_KEY, env_roots)
        if stored is not None and stored != env_roots:
            log.info("MEDIA_ROOTS в .env изменился, корни хранилища обновлены: %s", env_roots)
        return env_roots

    return stored


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_setting(db: Session, key: str, default: object = None) -> object:
    row = db.execute(select(Setting).where(Setting.key == key)).scalar_one_or_none()
    return json.loads(row.value) if row else default


def set_setting(db: Session, key: str, value: object) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=json.dumps(value)))
    else:
        row.value = json.dumps(value)


#: Типы SQLite для колонок, добавленных после первого релиза схемы.
_SQLITE_TYPES = {
    "INTEGER": {"INTEGER", "BIGINT", "BOOLEAN"},
}


def _add_missing_columns() -> None:
    """Догоняет схему БД под модели: ALTER TABLE ADD COLUMN для новых полей.

    Полноценные миграции (Alembic) не нужны, пока изменения сводятся
    к добавлению необязательных колонок; данные при этом не теряются.
    """
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in inspector.get_table_names():
                continue
            existing = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                if not column.nullable and column.default is None:
                    continue  # такую колонку без данных не добавить — нужна ручная миграция
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column.type.compile(engine.dialect)}"
                conn.execute(text(ddl))
                _backfill_default(conn, table.name, column)


def _backfill_default(conn, table_name: str, column) -> None:
    """SQLite добавляет колонку со значением NULL — существующие строки нужно дозаполнить.

    Иначе `has_audio` и подобные поля приходят в схему как None и валидация падает.
    """
    default = getattr(column.default, "arg", None) if column.default is not None else None
    if default is None or callable(default):
        return
    conn.execute(
        text(f"UPDATE {table_name} SET {column.name} = :value WHERE {column.name} IS NULL"),
        {"value": default},
    )


#: Колонки, которые не должны быть NULL: строки из прежних версий базы дозаполняем.
_NOT_NULL_DEFAULTS = {
    "media_files": {
        "has_meta": 0, "missing": 0, "has_audio": 0, "has_cover_art": 0,
    },
    "folders": {"is_root": 0, "missing": 0},
}


def _repair_nulls() -> None:
    existing = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        for table, columns in _NOT_NULL_DEFAULTS.items():
            if table not in existing:
                continue
            for column, value in columns.items():
                conn.execute(
                    text(f"UPDATE {table} SET {column} = :value WHERE {column} IS NULL"),
                    {"value": value},
                )
