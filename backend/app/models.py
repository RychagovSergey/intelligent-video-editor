"""Схема базы данных (ТЗ п. 4.1)."""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey, Index, LargeBinary, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enum_value(value: object) -> str:
    """Значение перечисления как строка.

    Колонки `type` и `analysis_status` объявлены как String, поэтому из базы
    приходят обычные строки, а из кода — члены Enum. Единая точка приведения
    избавляет от `.value` там, где значение может быть и тем и другим.
    """
    return value.value if isinstance(value, enum.Enum) else str(value)


class Base(DeclarativeBase):
    pass


class MediaType(str, enum.Enum):
    video = "video"
    image = "image"
    audio = "audio"


class AnalysisStatus(str, enum.Enum):
    pending = "pending"        # meta.json нет — «не проанализирован»
    analyzed = "analyzed"      # meta.json есть и актуален
    stale = "stale"            # файл изменился после анализа
    failed = "failed"          # модель вернула ошибку (analysis_failed)
    corrupted = "corrupted"    # ffprobe не смог прочитать файл


class Folder(Base):
    __tablename__ = "folders"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("folders.id", ondelete="CASCADE"))
    is_root: Mapped[bool] = mapped_column(Boolean, default=False)
    missing: Mapped[bool] = mapped_column(Boolean, default=False)

    parent: Mapped["Folder | None"] = relationship(remote_side=[id], back_populates="children")
    children: Mapped[list["Folder"]] = relationship(back_populates="parent", cascade="all, delete-orphan")
    files: Mapped[list["MediaFile"]] = relationship(back_populates="folder", cascade="all, delete-orphan")


class MediaFile(Base):
    __tablename__ = "media_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    folder_id: Mapped[int] = mapped_column(ForeignKey("folders.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String, unique=True, index=True)
    type: Mapped[MediaType] = mapped_column(String(8), index=True)
    size: Mapped[int] = mapped_column(BigInteger)
    modified: Mapped[float] = mapped_column(Float)          # mtime, для инкрементального скана
    has_meta: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    meta_path: Mapped[str | None] = mapped_column(String)   # <файл>.meta.json (Р-1)
    analysis_status: Mapped[AnalysisStatus] = mapped_column(String(12), default=AnalysisStatus.pending, index=True)
    missing: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # --- технические характеристики из ffprobe (этап 2) ---
    duration: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column()
    height: Mapped[int | None] = mapped_column()
    fps: Mapped[float | None] = mapped_column(Float)
    video_codec: Mapped[str | None] = mapped_column(String)
    audio_codec: Mapped[str | None] = mapped_column(String)
    bitrate: Mapped[int | None] = mapped_column(BigInteger)
    has_audio: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Обложка внутри файла — по ней рисуется миниатюра аудио.
    has_cover_art: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Дата съёмки из метаданных файла; для хронологии надёжнее, чем `modified`.
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    probe_error: Mapped[str | None] = mapped_column(Text)

    folder: Mapped[Folder] = relationship(back_populates="files")
    meta: Mapped["MetaCache | None"] = relationship(
        back_populates="file", cascade="all, delete-orphan", uselist=False
    )


Index("ix_media_files_folder_type", MediaFile.folder_id, MediaFile.type)


class MetaCache(Base):
    """Копия meta.json в БД — чтобы искать без чтения диска."""
    __tablename__ = "meta_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("media_files.id", ondelete="CASCADE"), unique=True)
    meta_json: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String)   # какой VL-моделью получено (Р-9)
    source_mtime: Mapped[float | None] = mapped_column(Float)  # mtime сайдкара
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    file: Mapped[MediaFile] = relationship(back_populates="meta")


class Embedding(Base):
    """Вектор описания файла для семантического поиска агентом (Р-12)."""

    __tablename__ = "embeddings"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("media_files.id", ondelete="CASCADE"), index=True)
    model: Mapped[str] = mapped_column(String)
    text_hash: Mapped[str] = mapped_column(String)   # пересчитываем только при смене метаданных
    vector: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


UniqueConstraint(Embedding.file_id, Embedding.model)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    timeline_json: Mapped[str] = mapped_column(Text, default="{}")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


UniqueConstraint(Setting.key)
