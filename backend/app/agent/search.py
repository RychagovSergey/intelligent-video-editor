"""Поиск медиафайлов для агента (Р-12).

Семантика важнее совпадения слов: запрос «клипы с людьми у воды» должен находить
кадры с описанием «мужчина на пляже». Поэтому описания из `meta_cache` переводятся
в векторы, а поиск считает косинусную близость. Если эмбеддинги недоступны
(нет ключа или сети), поиск честно падает обратно на поиск по подстроке.
"""
from __future__ import annotations

import array
import hashlib
import json
import logging
import math
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AnalysisStatus, Embedding, MediaFile, MediaType, MetaCache

log = logging.getLogger(__name__)

EMBED_URL = "https://api.openai.com/v1/embeddings"
BATCH = 64
#: Сколько текста описания уходит агенту. Вектор считается по полному тексту,
#: обрезка нужна лишь чтобы не раздувать контекст на выдаче в десятки файлов.
SUMMARY_LIMIT = 400


class EmbeddingsUnavailable(RuntimeError):
    pass


@dataclass
class Hit:
    file: MediaFile
    score: float
    summary: str


def searchable_text(meta_json: str | None, filename: str) -> str:
    """Собирает из meta.json короткий текст, по которому имеет смысл искать."""
    parts: list[str] = [filename]
    if not meta_json:
        return filename
    try:
        data = json.loads(meta_json)
    except json.JSONDecodeError:
        return filename
    if not isinstance(data, dict):
        return filename

    for key in ("description", "summary", "style", "emotions", "title_hint"):
        value = data.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    if isinstance(data.get("bpm"), (int, float)) and data["bpm"]:
        parts.append(f"{data['bpm']:g} BPM")
    for key in ("objects", "colors"):
        value = data.get(key)
        if isinstance(value, list):
            parts.extend(str(v) for v in value)
    for scene in data.get("scenes", []) or []:
        if isinstance(scene, dict) and scene.get("description"):
            parts.append(str(scene["description"]))
    return " · ".join(dict.fromkeys(p.strip() for p in parts if str(p).strip()))


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _pack(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def _unpack(blob: bytes) -> list[float]:
    values = array.array("f")
    values.frombytes(blob)
    return list(values)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def embed(texts: list[str]) -> list[list[float]]:
    if not settings.openai_api_key or not settings.openai_embed_model:
        raise EmbeddingsUnavailable("Не задан OPENAI_API_KEY или OPENAI_EMBED_MODEL")
    try:
        response = httpx.post(
            EMBED_URL,
            json={"model": settings.openai_embed_model, "input": texts},
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            timeout=60.0,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        raise EmbeddingsUnavailable(f"Сервис эмбеддингов недоступен: {exc}") from exc
    return [item["embedding"] for item in data["data"]]


def reindex(db: Session, *, force: bool = False) -> dict:
    """Досчитывает векторы для файлов, у которых их ещё нет или изменились метаданные."""
    model = settings.openai_embed_model
    rows = db.execute(
        select(MediaFile, MetaCache)
        .join(MetaCache, MetaCache.file_id == MediaFile.id, isouter=True)
        .where(MediaFile.missing.is_(False))
    ).all()

    existing = {
        (e.file_id, e.model): e
        for e in db.execute(select(Embedding).where(Embedding.model == model)).scalars()
    }

    pending: list[tuple[MediaFile, str, str]] = []
    for file, meta in rows:
        text = searchable_text(meta.meta_json if meta else None, file.filename)
        digest = _hash(text)
        current = existing.get((file.id, model))
        if current is not None and current.text_hash == digest and not force:
            continue
        pending.append((file, text, digest))

    indexed = 0
    for start in range(0, len(pending), BATCH):
        chunk = pending[start:start + BATCH]
        vectors = embed([text for _, text, _ in chunk])
        for (file, _text, digest), vector in zip(chunk, vectors):
            row = existing.get((file.id, model))
            if row is None:
                db.add(Embedding(file_id=file.id, model=model, text_hash=digest, vector=_pack(vector)))
            else:
                row.text_hash = digest
                row.vector = _pack(vector)
            indexed += 1
        db.flush()

    return {"indexed": indexed, "total": len(rows), "model": model}


def _keyword_search(db: Session, query: str, candidates: list[tuple[MediaFile, MetaCache | None]]) -> list[Hit]:
    words = [w.lower() for w in query.split() if len(w) > 2]
    hits: list[Hit] = []
    for file, meta in candidates:
        text = searchable_text(meta.meta_json if meta else None, file.filename)
        lowered = text.lower()
        score = sum(1 for w in words if w in lowered) / max(len(words), 1)
        if score > 0:
            hits.append(Hit(file=file, score=score, summary=text[:SUMMARY_LIMIT]))
    return sorted(hits, key=lambda h: h.score, reverse=True)


def _bpm_of(meta: MetaCache | None) -> float | None:
    """Темп из meta.json, если файл размечен при анализе."""
    if meta is None or not meta.meta_json:
        return None
    try:
        data = json.loads(meta.meta_json)
    except json.JSONDecodeError:
        return None
    bpm = data.get("bpm") if isinstance(data, dict) else None
    return float(bpm) if isinstance(bpm, (int, float)) and bpm else None


def search(
    db: Session,
    query: str,
    *,
    media_type: MediaType | None = None,
    min_duration: float | None = None,
    max_duration: float | None = None,
    min_bpm: float | None = None,
    max_bpm: float | None = None,
    limit: int = 10,
    exclude_ids: set[int] | None = None,
) -> tuple[list[Hit], str]:
    """Возвращает подходящие файлы и способ поиска: `semantic` или `keyword`.

    `exclude_ids` — обычно файлы, уже стоящие на таймлайне: без этого лучшие по смыслу
    результаты на большой библиотеке заняты уже добавленным материалом, и агенту
    приходится задирать `limit` (20→50→97...), просто чтобы докопаться до чего-то
    нового — вместо этого исключаем занятое прямо в запросе.
    """
    stmt = (
        select(MediaFile, MetaCache)
        .join(MetaCache, MetaCache.file_id == MediaFile.id, isouter=True)
        .where(MediaFile.missing.is_(False), MediaFile.analysis_status != AnalysisStatus.corrupted)
    )
    if media_type is not None:
        stmt = stmt.where(MediaFile.type == media_type)
    if min_duration is not None:
        stmt = stmt.where(MediaFile.duration >= min_duration)
    if max_duration is not None:
        stmt = stmt.where(MediaFile.duration <= max_duration)
    if exclude_ids:
        stmt = stmt.where(MediaFile.id.not_in(exclude_ids))

    candidates = [(f, m) for f, m in db.execute(stmt).all()]
    if min_bpm is not None or max_bpm is not None:
        # Темп лежит в meta.json, а не колонкой, поэтому отсеиваем уже после выборки.
        # Файл без размеченного темпа под запрос «трек 100–120 BPM» не подходит.
        candidates = [
            (f, m) for f, m in candidates
            if (bpm := _bpm_of(m)) is not None
            and (min_bpm is None or bpm >= min_bpm)
            and (max_bpm is None or bpm <= max_bpm)
        ]
    if not candidates:
        return [], "empty"

    try:
        reindex(db)
        query_vector = embed([query])[0]
    except EmbeddingsUnavailable as exc:
        log.info("Семантический поиск недоступен (%s), ищем по словам", exc)
        return _keyword_search(db, query, candidates)[:limit], "keyword"

    vectors = {
        e.file_id: _unpack(e.vector)
        for e in db.execute(
            select(Embedding).where(Embedding.model == settings.openai_embed_model)
        ).scalars()
    }

    hits = []
    for file, meta in candidates:
        vector = vectors.get(file.id)
        if vector is None:
            continue
        hits.append(
            Hit(
                file=file,
                score=round(_cosine(query_vector, vector), 4),
                summary=searchable_text(meta.meta_json if meta else None, file.filename)[:SUMMARY_LIMIT],
            )
        )
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit], "semantic"
