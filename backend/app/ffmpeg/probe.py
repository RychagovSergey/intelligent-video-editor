"""Технические характеристики файла через ffprobe (ТЗ пп. 3.2, 5)."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from .runner import BinaryMissing, run

log = logging.getLogger(__name__)

#: ffprobe отдаёт для картинок фиктивные fps и «поток видео» — эти значения не нужны.
IMAGE_CODECS = {"png", "mjpeg", "webp", "bmp", "gif", "tiff", "hevc_still", "heif", "avif", "jpeg2000"}


@dataclass
class TechInfo:
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    bitrate: int | None = None
    has_audio: bool = False
    has_cover_art: bool = False   # обложка внутри аудиофайла — не видеопоток
    rotation: int = 0
    #: Дата съёмки из метаданных контейнера. Надёжнее mtime: тот меняется при копировании.
    captured_at: datetime | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def orientation(self) -> str | None:
        """Вертикальное, горизонтальное или квадратное — агенту так понятнее пропорций."""
        if not self.width or not self.height:
            return None
        if self.width > self.height:
            return "horizontal"
        return "vertical" if self.width < self.height else "square"

    @property
    def resolution(self) -> str | None:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return None

    def as_dict(self) -> dict:
        data = asdict(self)
        data["resolution"] = self.resolution
        data["orientation"] = self.orientation
        return data


def _to_float(value: object) -> float | None:
    try:
        result = float(str(value))
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _to_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _fps(stream: dict) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key)
        if not raw or raw in ("0/0", "0"):
            continue
        try:
            value = float(Fraction(raw))
        except (ValueError, ZeroDivisionError):
            continue
        if value > 0:
            return round(value, 3)
    return None


def _captured_at(fmt: dict, streams: list[dict]) -> datetime | None:
    """`creation_time` пишут камеры и телефоны; у скопированных файлов mtime уже не годится."""
    candidates = [(fmt.get("tags") or {}).get("creation_time")]
    candidates += [(s.get("tags") or {}).get("creation_time") for s in streams]
    for raw in candidates:
        if not raw:
            continue
        try:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value
    return None


def _rotation(stream: dict) -> int:
    """Поворот из display matrix — иначе вертикальные видео с iPhone обрабатываются боком."""
    for side in stream.get("side_data_list", []) or []:
        if "rotation" in side:
            value = _to_int(side["rotation"])
            if value is not None:
                return int(value) % 360
    tag = stream.get("tags", {}).get("rotate")
    value = _to_int(tag) if tag else None
    return value % 360 if value is not None else 0


def probe(path: Path | str) -> TechInfo:
    """Читает характеристики файла. При ошибке возвращает TechInfo с полем error."""
    try:
        result = run(
            "ffprobe",
            [
                "-v", "error",
                "-show_entries",
                "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,duration,bit_rate:"
                "stream_disposition=attached_pic:stream_side_data=rotation:"
                "stream_tags=rotate,creation_time:"
                "format=duration,bit_rate:format_tags=creation_time",
                "-of", "json",
                str(path),
            ],
            timeout=30.0,
        )
    except BinaryMissing as exc:
        return TechInfo(error=str(exc))

    if not result.ok:
        message = (result.stderr or "ffprobe завершился с ошибкой").strip().splitlines()
        return TechInfo(error=message[-1] if message else "ffprobe завершился с ошибкой")

    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return TechInfo(error=f"Не удалось разобрать вывод ffprobe: {exc}")

    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    if not streams:
        return TechInfo(error="Файл не содержит распознаваемых потоков")

    # Обложка в mp3 приходит как видеопоток attached_pic — иначе трек выглядит как видео 90000 fps.
    def _is_cover(stream: dict) -> bool:
        return bool((stream.get("disposition") or {}).get("attached_pic"))

    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not _is_cover(s)), None
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    cover = next((s for s in streams if s.get("codec_type") == "video" and _is_cover(s)), None)

    info = TechInfo(
        captured_at=_captured_at(fmt, streams),
        duration=_to_float(fmt.get("duration")) or (_to_float(video.get("duration")) if video else None),
        bitrate=_to_int(fmt.get("bit_rate")),
        audio_codec=audio.get("codec_name") if audio else None,
        has_audio=audio is not None,
        has_cover_art=cover is not None,
    )

    if video is not None:
        info.video_codec = video.get("codec_name")
        info.width = _to_int(video.get("width"))
        info.height = _to_int(video.get("height"))
        info.fps = _fps(video)
        info.rotation = _rotation(video)
        if info.rotation in (90, 270) and info.width and info.height:
            info.width, info.height = info.height, info.width
        if info.duration is None:
            info.duration = _to_float(video.get("duration"))

    if info.duration is None and audio is not None:
        info.duration = _to_float(audio.get("duration"))

    if not info.has_audio and info.duration is None and (info.video_codec or "") in IMAGE_CODECS:
        info.fps = None  # статичное изображение

    return info
