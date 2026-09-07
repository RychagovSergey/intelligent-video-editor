"""Корни хранилища, скан, дерево папок (ТЗ п. 3.1)."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import expand
from ..db import MEDIA_ROOTS_KEY, get_db, get_setting, set_setting
from ..jobs import scan_job
from ..models import Folder, MediaFile
from ..schemas import FolderOut, RootInfo, RootsIn, RootsOut, ScanStatusOut

router = APIRouter(prefix="/api", tags=["storage"])


def _roots(db: Session) -> list[Path]:
    return [Path(p) for p in (get_setting(db, MEDIA_ROOTS_KEY, []) or [])]


def _describe(path: Path) -> RootInfo:
    exists = path.exists()
    is_dir = path.is_dir() if exists else False
    return RootInfo(
        path=str(path),
        exists=exists,
        is_dir=is_dir,
        writable=is_dir and os.access(path, os.W_OK),
    )


@router.get("/storage/roots", response_model=RootsOut)
def list_roots(db: Session = Depends(get_db)) -> RootsOut:
    return RootsOut(roots=[_describe(p) for p in _roots(db)])


@router.put("/storage/roots", response_model=RootsOut)
def set_roots(payload: RootsIn, db: Session = Depends(get_db)) -> RootsOut:
    """Папки, заданные в UI, имеют приоритет над MEDIA_ROOTS из .env (Р-11)."""
    resolved = [expand(p) for p in payload.roots if p.strip()]
    missing = [str(p) for p in resolved if not p.is_dir()]
    if missing:
        raise HTTPException(400, detail=f"Папки не найдены: {', '.join(missing)}")
    set_setting(db, MEDIA_ROOTS_KEY, [str(p) for p in resolved])
    _drop_orphan_index(db, resolved)
    return RootsOut(roots=[_describe(p) for p in resolved])


def _drop_orphan_index(db: Session, roots: list[Path]) -> None:
    """Убирает из индекса всё, что больше не принадлежит ни одному корню.

    Иначе после смены корня в дереве остаются папки, которых в хранилище уже нет.
    Файлы и meta_cache удаляются каскадом; сами файлы на диске не трогаем.
    """
    prefixes = [str(r) for r in roots]
    for folder in db.execute(select(Folder)).scalars().all():
        if not any(folder.path == p or folder.path.startswith(p + "/") for p in prefixes):
            db.delete(folder)


@router.post("/scan", response_model=ScanStatusOut)
def start_scan(db: Session = Depends(get_db)) -> ScanStatusOut:
    roots = _roots(db)
    if not roots:
        raise HTTPException(400, detail="Не задана ни одна папка хранилища")
    if not scan_job.start(roots):
        raise HTTPException(409, detail="Скан уже выполняется")
    return ScanStatusOut(**scan_job.snapshot())


@router.get("/scan/status", response_model=ScanStatusOut)
def scan_status() -> ScanStatusOut:
    return ScanStatusOut(**scan_job.snapshot())


@router.post("/probe")
def start_probe(force: bool = False, db: Session = Depends(get_db)) -> dict:
    """Перечитать технические характеристики (по умолчанию — только для новых файлов)."""
    from ..probing import probe_pending
    return probe_pending(db, force=force)


@router.get("/folders", response_model=list[FolderOut])
def list_folders(parent_id: int | None = None, db: Session = Depends(get_db)) -> list[FolderOut]:
    """Дети указанной папки; без parent_id — корни."""
    stmt = select(Folder).where(
        Folder.parent_id.is_(None) if parent_id is None else Folder.parent_id == parent_id
    ).order_by(Folder.name)
    folders = list(db.execute(stmt).scalars())
    if not folders:
        return []

    ids = [f.id for f in folders]
    counts = dict(
        db.execute(
            select(MediaFile.folder_id, func.count(MediaFile.id))
            .where(MediaFile.folder_id.in_(ids), MediaFile.missing.is_(False))
            .group_by(MediaFile.folder_id)
        ).all()
    )
    with_children = {
        pid for (pid,) in db.execute(
            select(Folder.parent_id).where(Folder.parent_id.in_(ids)).distinct()
        ).all()
    }

    out = []
    for f in folders:
        item = FolderOut.model_validate(f)
        item.file_count = counts.get(f.id, 0)
        item.has_children = f.id in with_children
        out.append(item)
    return out
