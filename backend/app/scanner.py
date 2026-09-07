"""Сканирование хранилища и индексация (ТЗ п. 3.1).

Скан инкрементальный: файл переиндексируется, только если изменились size/mtime.
Существующие сайдкары подхватываются, повторный анализ не запускается (ТЗ п. 3.1, п. 5).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .media_types import media_type, should_skip_dir
from .models import AnalysisStatus, Folder, MediaFile, MetaCache
from .paths import is_inside_cache, legacy_meta_path_for, meta_path_for

log = logging.getLogger(__name__)


@dataclass
class ScanStats:
    roots: list[str] = field(default_factory=list)
    folders: int = 0
    files_total: int = 0
    files_added: int = 0
    files_updated: int = 0
    files_missing: int = 0
    meta_loaded: int = 0
    unreadable: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "roots": self.roots,
            "folders": self.folders,
            "files_total": self.files_total,
            "files_added": self.files_added,
            "files_updated": self.files_updated,
            "files_missing": self.files_missing,
            "meta_loaded": self.meta_loaded,
            "unreadable": self.unreadable[:50],
            "errors": self.errors[:50],
        }


def _upsert_folder(db: Session, path: Path, parent: Folder | None, is_root: bool) -> Folder:
    folder = db.execute(select(Folder).where(Folder.path == str(path))).scalar_one_or_none()
    if folder is None:
        folder = Folder(
            path=str(path),
            name=path.name or str(path),
            parent_id=parent.id if parent else None,
            is_root=is_root,
        )
        db.add(folder)
        db.flush()
    else:
        folder.parent_id = parent.id if parent else None
        folder.is_root = is_root
        folder.missing = False
    return folder


def _load_sidecar(db: Session, file: MediaFile, sidecar: Path) -> bool:
    """Читает <файл>.meta.json в meta_cache. Возвращает True, если данные обновились."""
    try:
        stat = sidecar.stat()
        cached = file.meta
        if cached is not None and cached.source_mtime == stat.st_mtime:
            return False
        raw = sidecar.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.warning("Не удалось прочитать сайдкар %s: %s", sidecar, exc)
        file.analysis_status = AnalysisStatus.failed
        return False

    model = data.get("model") if isinstance(data, dict) else None
    if cached is None:
        db.add(MetaCache(file_id=file.id, meta_json=raw, model=model, source_mtime=stat.st_mtime))
    else:
        cached.meta_json = raw
        cached.model = model
        cached.source_mtime = stat.st_mtime

    failed = isinstance(data, dict) and data.get("analysis_failed") is True
    file.analysis_status = AnalysisStatus.failed if failed else AnalysisStatus.analyzed
    return True


def _index_file(db: Session, folder: Folder, path: Path, stats: ScanStats) -> str | None:
    try:
        st = path.stat()
    except OSError as exc:
        stats.unreadable.append(f"{path}: {exc.strerror or exc}")
        return None

    kind = media_type(path)
    if kind is None:
        return None

    # Метаданные лежат в кеше хранилища; сайдкары старой схемы читаем, но не создаём.
    sidecar = meta_path_for(path)
    if not sidecar.exists():
        legacy = legacy_meta_path_for(path)
        if legacy.exists():
            sidecar = legacy
    has_sidecar = sidecar.exists()

    file = db.execute(select(MediaFile).where(MediaFile.path == str(path))).scalar_one_or_none()
    if file is None:
        file = MediaFile(
            folder_id=folder.id,
            filename=path.name,
            path=str(path),
            type=kind,
            size=st.st_size,
            modified=st.st_mtime,
            has_meta=has_sidecar,
            meta_path=str(sidecar) if has_sidecar else None,
            analysis_status=AnalysisStatus.pending,
        )
        db.add(file)
        db.flush()
        stats.files_added += 1
    else:
        changed = file.size != st.st_size or file.modified != st.st_mtime
        file.folder_id = folder.id
        file.filename = path.name
        file.type = kind
        file.missing = False
        file.has_meta = has_sidecar
        file.meta_path = str(sidecar) if has_sidecar else None
        if changed:
            file.size = st.st_size
            file.modified = st.st_mtime
            # Файл переснят/перезаписан — прежние метаданные больше не описывают его.
            if file.analysis_status == AnalysisStatus.analyzed:
                file.analysis_status = AnalysisStatus.stale
            stats.files_updated += 1

    if has_sidecar:
        if _load_sidecar(db, file, sidecar):
            stats.meta_loaded += 1
    else:
        file.analysis_status = AnalysisStatus.pending

    stats.files_total += 1
    return str(path)


def scan_roots(db: Session, roots: list[Path]) -> ScanStats:
    """Обходит корни рекурсивно и синхронизирует индекс с диском."""
    stats = ScanStats(roots=[str(r) for r in roots])
    seen: set[str] = set()
    seen_folders: set[int] = set()

    for root in roots:
        if not root.exists():
            stats.errors.append(f"Папка не найдена: {root}")
            continue
        if not root.is_dir():
            stats.errors.append(f"Не является папкой: {root}")
            continue

        root_folder = _upsert_folder(db, root, None, is_root=True)
        seen_folders.add(root_folder.id)
        stats.folders += 1
        queue: list[tuple[Path, Folder]] = [(root, root_folder)]

        while queue:
            current, folder = queue.pop()
            try:
                entries = sorted(current.iterdir())
            except OSError as exc:
                stats.unreadable.append(f"{current}: {exc.strerror or exc}")
                continue

            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    # Папка кеша лежит внутри хранилища — индексировать её содержимое нельзя.
                    if should_skip_dir(entry) or is_inside_cache(entry):
                        continue
                    child = _upsert_folder(db, entry, folder, is_root=False)
                    seen_folders.add(child.id)
                    stats.folders += 1
                    queue.append((entry, child))
                elif entry.is_file():
                    indexed = _index_file(db, folder, entry, stats)
                    if indexed:
                        seen.add(indexed)

    _mark_missing(db, roots, seen, seen_folders, stats)
    _prune_empty_folders(db, seen_folders)
    db.flush()
    return stats


def _mark_missing(
    db: Session, roots: list[Path], seen: set[str], seen_folders: set[int], stats: ScanStats
) -> None:
    """Пропавшие файлы помечаются, а не удаляются — метаданные и проекты на них ссылаются."""
    prefixes = [str(r) for r in roots if r.exists()]
    if not prefixes:
        return
    for file in db.execute(select(MediaFile)).scalars():
        if file.path in seen:
            continue
        if not any(file.path.startswith(p) for p in prefixes):
            continue  # файл из другого корня, который сейчас не сканировали
        if not file.missing:
            file.missing = True
        stats.files_missing += 1
    for folder in db.execute(select(Folder)).scalars():
        if folder.id in seen_folders:
            continue
        if any(folder.path.startswith(p) for p in prefixes):
            folder.missing = True


def _prune_empty_folders(db: Session, seen_folders: set[int]) -> None:
    """Убирает из дерева папки без медиафайлов — чтобы дерево не заполнялось шумом."""
    folders = {f.id: f for f in db.execute(select(Folder)).scalars()}
    has_files = {
        fid for (fid,) in db.execute(
            select(MediaFile.folder_id).where(MediaFile.missing.is_(False)).distinct()
        )
    }
    keep: set[int] = set()
    for fid in has_files:
        node = folders.get(fid)
        while node is not None and node.id not in keep:
            keep.add(node.id)
            node = folders.get(node.parent_id) if node.parent_id else None
    for folder in folders.values():
        if folder.is_root:
            keep.add(folder.id)
    for folder in folders.values():
        if folder.id not in keep:
            db.delete(folder)
