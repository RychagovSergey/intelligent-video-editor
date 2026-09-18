"""Список медиафайлов и их метаданные (ТЗ пп. 3.1, 3.7)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..ffmpeg.render import play
from ..ffmpeg.runner import BinaryMissing
from ..ffmpeg.thumbs import THUMB_WIDTH, ensure_thumbnail
from ..ffmpeg.waveform import DEFAULT_POINTS, MAX_POINTS, MIN_POINTS, ensure_waveform
from ..models import AnalysisStatus, MediaFile, MediaType, MetaCache, enum_value
from ..schemas import MediaFileOut, MediaListOut, MetaOut

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["media"])


@router.get("/media", response_model=MediaListOut)
def list_media(
    folder_id: int | None = None,
    recursive: bool = False,
    type: MediaType | None = None,
    status: AnalysisStatus | None = None,
    q: str | None = None,
    include_missing: bool = False,
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> MediaListOut:
    stmt = select(MediaFile)
    count_stmt = select(func.count(MediaFile.id))

    conditions = []
    if folder_id is not None:
        if recursive:
            from ..models import Folder
            folder = db.get(Folder, folder_id)
            if folder is None:
                raise HTTPException(404, detail="Папка не найдена")
            conditions.append(MediaFile.path.startswith(folder.path + "/"))
        else:
            conditions.append(MediaFile.folder_id == folder_id)
    if type is not None:
        conditions.append(MediaFile.type == type)
    if status is not None:
        conditions.append(MediaFile.analysis_status == status)
    if not include_missing:
        conditions.append(MediaFile.missing.is_(False))
    if q:
        like = f"%{q}%"
        conditions.append(or_(MediaFile.filename.ilike(like), MediaFile.path.ilike(like)))

    for cond in conditions:
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = db.execute(count_stmt).scalar_one()
    rows = db.execute(
        stmt.order_by(MediaFile.filename).limit(limit).offset(offset)
    ).scalars().all()

    return MediaListOut(
        items=[MediaFileOut.model_validate(r) for r in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/media/{file_id}", response_model=MediaFileOut)
def get_media(file_id: int, db: Session = Depends(get_db)) -> MediaFileOut:
    file = db.get(MediaFile, file_id)
    if file is None:
        raise HTTPException(404, detail="Файл не найден")
    return MediaFileOut.model_validate(file)


@router.get("/media/{file_id}/meta", response_model=MetaOut)
def get_media_meta(file_id: int, db: Session = Depends(get_db)) -> MetaOut:
    file = db.get(MediaFile, file_id)
    if file is None:
        raise HTTPException(404, detail="Файл не найден")
    return MetaOut.from_cache(file_id, file.meta)


#: Поля meta.json, которые можно править руками (ТЗ п. 3.7). Технические поля
#: (длительность, разрешение, сцены с таймкодами, ритм) — только от анализа.
EDITABLE_TEXT = {"description", "summary", "style", "emotions", "quality", "title_hint"}
EDITABLE_LISTS = {"objects", "colors"}
#: У сцен правится только описание: время и качество кадра — от анализа.
EDITABLE_SCENE = {"description"}


class MetaPatch(BaseModel):
    fields: dict[str, Any] = {}
    #: Индекс сцены → новые значения её полей.
    scenes: dict[int, dict[str, Any]] = {}


def _clean_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise HTTPException(400, detail="Ожидается список строк")
    return [str(v).strip() for v in value if str(v).strip()]


@router.put("/media/{file_id}/meta", response_model=MetaOut)
def update_media_meta(file_id: int, patch: MetaPatch, db: Session = Depends(get_db)) -> MetaOut:
    """Ручная правка описаний в meta.json.

    Правится и сайдкар, и копия в базе — как после анализа. Эмбеддинг для поиска
    пересчитается сам: `search_media` перед поиском сверяет хеш текста (см. agent/search.py).
    Повторный анализ с `force` перепишет правки — на это интерфейс отдельно указывает.
    """
    from ..analysis.analyzer import write_sidecar

    file = db.get(MediaFile, file_id)
    if file is None:
        raise HTTPException(404, detail="Файл не найден")
    cache = db.execute(select(MetaCache).where(MetaCache.file_id == file_id)).scalar_one_or_none()
    if cache is None:
        raise HTTPException(400, detail="Файл ещё не проанализирован — править нечего")
    try:
        meta = json.loads(cache.meta_json)
    except json.JSONDecodeError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}

    for key, value in patch.fields.items():
        if key in EDITABLE_TEXT:
            meta[key] = "" if value is None else str(value).strip()
        elif key in EDITABLE_LISTS:
            meta[key] = _clean_list(value)
        else:
            raise HTTPException(400, detail=f"Поле «{key}» не редактируется")
    scenes = meta.get("scenes")
    for index, values in patch.scenes.items():
        if not isinstance(scenes, list) or not 0 <= index < len(scenes):
            raise HTTPException(400, detail=f"Сцены {index} нет")
        for key, value in values.items():
            if key not in EDITABLE_SCENE:
                raise HTTPException(400, detail=f"Поле сцены «{key}» не редактируется")
            scenes[index][key] = "" if value is None else str(value).strip()

    meta["edited_at"] = datetime.now(timezone.utc).isoformat()
    raw = json.dumps(meta, ensure_ascii=False)
    try:
        sidecar = write_sidecar(Path(file.path), meta)
        cache.source_mtime = sidecar.stat().st_mtime
        file.meta_path = str(sidecar)
        file.has_meta = True
    except OSError as exc:
        # Папка только на чтение — правка остаётся в базе, как и при анализе.
        log.warning("meta.json не записан для %s: %s", file.path, exc)
    cache.meta_json = raw
    db.flush()
    return MetaOut.from_cache(file_id, cache)


#: Эти форматы браузер играет сам, остальные показываем перекодированной картинкой.
WEB_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
WEB_MEDIA_EXT = {".mp4", ".m4v", ".mov", ".webm", ".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac"}


@router.get("/media/{file_id}/file")
def get_media_file(file_id: int, db: Session = Depends(get_db)) -> FileResponse:
    """Отдаёт исходный файл для просмотра в приложении (перемотка — через Range-запросы)."""
    file = db.get(MediaFile, file_id)
    if file is None:
        raise HTTPException(404, detail="Файл не найден")
    path = Path(file.path)
    if not path.exists():
        raise HTTPException(404, detail="Файл пропал с диска")
    return FileResponse(path, filename=file.filename)


@router.get("/media/{file_id}/playable")
def is_playable(file_id: int, db: Session = Depends(get_db)) -> dict:
    """Сможет ли браузер показать файл сам, или нужна перекодированная картинка."""
    file = db.get(MediaFile, file_id)
    if file is None:
        raise HTTPException(404, detail="Файл не найден")
    suffix = Path(file.path).suffix.lower()
    kind = enum_value(file.type)
    return {
        "kind": kind,
        "extension": suffix,
        "native": suffix in (WEB_IMAGE_EXT if kind == "image" else WEB_MEDIA_EXT),
    }


@router.post("/media/{file_id}/play")
def play_in_ffplay(file_id: int, db: Session = Depends(get_db)) -> dict:
    """Открыть файл отдельным окном ffplay — запасной путь для форматов, чуждых браузеру."""
    file = db.get(MediaFile, file_id)
    if file is None:
        raise HTTPException(404, detail="Файл не найден")
    try:
        play(Path(file.path), title=file.filename)
    except BinaryMissing as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return {"playing": file.filename}


@router.get("/media/{file_id}/thumbnail")
def get_thumbnail(file_id: int, width: int = THUMB_WIDTH, db: Session = Depends(get_db)) -> Response:
    """Миниатюра или увеличенное превью; создаётся при первом запросе и кешируется на диске."""
    file = db.get(MediaFile, file_id)
    if file is None:
        raise HTTPException(404, detail="Файл не найден")
    thumb = ensure_thumbnail(Path(file.path), file.type, file.size, file.modified, width)
    if thumb is None:
        raise HTTPException(404, detail="Миниатюра недоступна")
    return FileResponse(thumb, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@router.get("/media/{file_id}/waveform")
def get_waveform(
    file_id: int, points: int = Query(DEFAULT_POINTS, ge=MIN_POINTS, le=MAX_POINTS),
    db: Session = Depends(get_db),
) -> dict:
    """Огибающая амплитуды для отрисовки waveform на таймлайне; кешируется на диске."""
    file = db.get(MediaFile, file_id)
    if file is None:
        raise HTTPException(404, detail="Файл не найден")
    if enum_value(file.type) != "audio":
        raise HTTPException(400, detail="Waveform доступен только для аудиофайлов")
    if not file.duration:
        raise HTTPException(404, detail="Длительность файла неизвестна")
    path = Path(file.path)
    if not path.exists():
        raise HTTPException(404, detail="Файл пропал с диска")

    values = ensure_waveform(path, file.modified, file.duration, points)
    return {"duration": file.duration, "points": values}


@router.get("/stats")
def storage_stats(db: Session = Depends(get_db)) -> dict:
    """Сводка для нижней панели: сколько файлов и сколько ещё не проанализировано."""
    by_type = dict(
        db.execute(
            select(MediaFile.type, func.count(MediaFile.id))
            .where(MediaFile.missing.is_(False))
            .group_by(MediaFile.type)
        ).all()
    )
    by_status = dict(
        db.execute(
            select(MediaFile.analysis_status, func.count(MediaFile.id))
            .where(MediaFile.missing.is_(False))
            .group_by(MediaFile.analysis_status)
        ).all()
    )
    return {
        "total": sum(by_type.values()),
        "by_type": {enum_value(k): v for k, v in by_type.items()},
        "by_status": {enum_value(k): v for k, v in by_status.items()},
    }
