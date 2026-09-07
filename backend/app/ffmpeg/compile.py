"""Сборка команды ffmpeg из таймлайна (ТЗ пп. 4.4, 3.6).

Один и тот же компилятор используется предпросмотром и экспортом — различаются
только пресетом кодирования (Р-6). Так превью не расходится с итоговым файлом.
"""
from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import MediaFile
from ..timeline.schema import TRANSITION_KINDS, Clip, Timeline
from .textlayer import render_text_png

log = logging.getLogger(__name__)

MIN_GAP = 0.01                # промежутки короче считаем стыком, а не дырой
ADJACENCY_TOLERANCE = 0.01    # тот же допуск, что у ops.ADJACENCY_TOLERANCE
ATEMPO_MIN = 0.5              # ограничение фильтра atempo, за его пределами фильтры сцепляются
ATEMPO_MAX = 2.0
TEXT_FADE_SECONDS = 0.4       # фиксированная длительность fade in/out у текстового слоя


class CompileError(RuntimeError):
    """Таймлайн нельзя собрать: нет исходников, пустой проект и т. п."""


@dataclass
class RenderPreset:
    name: str
    width: int
    height: int
    fps: float
    video_args: list[str]
    audio_args: list[str] = field(default_factory=lambda: ["-c:a", "aac", "-b:a", "192k"])
    container_args: list[str] = field(default_factory=lambda: ["-movflags", "+faststart"])


#: Качество экспорта (ТЗ п. 3.6): CRF ниже — файл больше и чётче.
QUALITY_CRF = {"high": 18, "medium": 23, "low": 28}

#: ProRes-профиль для MOV: 2 — стандартный, хороший баланс размера и качества.
PRORES_PROFILE = "2"


def export_preset(
    *,
    container: str = "mp4",
    quality: str = "medium",
    width: int = 1920,
    height: int = 1080,
    fps: float = 30.0,
    bitrate: str | None = None,
) -> "RenderPreset":
    """Пресет для итогового рендера (ТЗ п. 3.6): MP4/H.264, MOV/ProRes или WebM/VP9."""
    if container == "mov":
        video_args = ["-c:v", "prores_ks", "-profile:v", PRORES_PROFILE, "-pix_fmt", "yuv422p10le"]
        audio_args = ["-c:a", "pcm_s16le"]
        container_args: list[str] = []
    elif container == "webm":
        video_args = ["-c:v", "libvpx-vp9", "-crf", str(QUALITY_CRF.get(quality, 23)), "-b:v", "0",
                      "-pix_fmt", "yuv420p"]
        audio_args = ["-c:a", "libopus", "-b:a", "160k"]
        container_args = []
    else:
        video_args = ["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]
        if bitrate:
            # Заданный битрейт важнее пресета качества — так просил ТЗ п. 3.6.
            video_args += ["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bitrate]
        else:
            video_args += ["-crf", str(QUALITY_CRF.get(quality, 23))]
        audio_args = ["-c:a", "aac", "-b:a", "192k"]
        container_args = ["-movflags", "+faststart"]

    return RenderPreset(
        name=f"{container}-{quality}",
        width=width, height=height, fps=fps,
        video_args=video_args, audio_args=audio_args, container_args=container_args,
    )


#: Предпросмотр: маленький и быстрый — важна скорость подготовки, а не качество (ТЗ п. 3.4).
PREVIEW_PRESET = RenderPreset(
    name="preview",
    width=640, height=360, fps=25.0,
    video_args=["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p"],
    audio_args=["-c:a", "aac", "-b:a", "128k"],
)


@dataclass
class Command:
    args: list[str]
    duration: float
    has_audio: bool

    def as_shell(self) -> str:
        return " ".join(self.args)


def atempo_chain(speed: float) -> list[str]:
    """`atempo` работает в диапазоне 0.5–2.0, поэтому крайние скорости набираем цепочкой."""
    if abs(speed - 1.0) < 1e-6:
        return []
    factors: list[float] = []
    remaining = speed
    while remaining > ATEMPO_MAX:
        factors.append(ATEMPO_MAX)
        remaining /= ATEMPO_MAX
    while remaining < ATEMPO_MIN:
        factors.append(ATEMPO_MIN)
        remaining /= ATEMPO_MIN
    factors.append(remaining)
    return [f"atempo={f:.6f}" for f in factors]


def _sources(db: Session, timeline: Timeline) -> dict[int, MediaFile]:
    # Текстовые клипы не ссылаются на медиафайл (source_id пуст) — их сюда не берём.
    ids = {
        clip.source_id
        for track in timeline.tracks if track.kind != "text"
        for clip in track.clips
    }
    if not ids:
        raise CompileError("Таймлайн пуст — нечего собирать")
    files = {f.id: f for f in db.query(MediaFile).filter(MediaFile.id.in_(ids)).all()}
    missing = [i for i in ids if i not in files or not Path(files[i].path).exists()]
    if missing:
        names = ", ".join(str(files[i].filename if i in files else i) for i in missing)
        raise CompileError(f"Исходники недоступны: {names}")
    return files


def build_command(
    db: Session,
    timeline: Timeline,
    output: Path,
    preset: RenderPreset = PREVIEW_PRESET,
) -> Command:
    files = _sources(db, timeline)

    width, height, fps = preset.width, preset.height, preset.fps
    inputs: list[str] = []
    filters: list[str] = []
    # Три списка идут parallel-по-индексу: на каждый сегмент видеоряда (клип или чёрная
    # заглушка на пустоте) — его лейбл фильтра, длительность и сам Clip (None у заглушки).
    video_labels: list[str] = []
    video_durations: list[float] = []
    video_clips: list[Clip | None] = []
    audio_labels: list[str] = []

    video_track = timeline.first_track("video")
    audio_track = timeline.first_track("audio")
    text_track = timeline.first_track("text")

    position = 0.0
    segment = 0

    for clip in (video_track.sorted_clips() if video_track else []):
        gap = clip.start - position
        if gap > MIN_GAP:
            # Пустоту заполняем чёрным кадром, иначе клипы «съезжают» по времени.
            label = f"v{segment}"
            inputs += ["-f", "lavfi", "-t", f"{gap:.4f}",
                       "-i", f"color=c=black:s={width}x{height}:r={fps}"]
            filters.append(f"[{_last_input(inputs)}:v]setsar=1,format=yuv420p[{label}]")
            video_labels.append(label)
            video_durations.append(gap)
            video_clips.append(None)
            segment += 1
            position = clip.start

        index = _add_visual_input(inputs, files[clip.source_id], clip)
        label = f"v{segment}"
        filters.append(_video_filter(index, clip, label, width, height, fps))
        video_labels.append(label)
        video_durations.append(clip.duration)
        video_clips.append(clip)
        segment += 1
        position = clip.end

        source = files[clip.source_id]
        if clip.kind == "video" and source.has_audio and not clip.muted:
            audio_labels.append(_audio_filter(filters, index, clip, f"a{index}"))

    for clip in (audio_track.sorted_clips() if audio_track else []):
        if clip.muted:
            continue
        index = _add_audio_input(inputs, files[clip.source_id], clip)
        audio_labels.append(_audio_filter(filters, index, clip, f"a{index}"))

    if not video_labels:
        raise CompileError("На видеодорожке нет клипов")

    vout = _fold_video_chain(filters, video_labels, video_durations, video_clips, fps=fps)
    vout = _apply_text_overlays(
        filters, inputs, vout, text_track.sorted_clips() if text_track else [],
        width=width, height=height,
    )
    filters.append(f"[{vout}]null[vout]")

    has_audio = bool(audio_labels)
    if has_audio:
        if len(audio_labels) == 1:
            filters.append(f"[{audio_labels[0]}]anull[aout]")
        else:
            filters.append(
                "".join(f"[{a}]" for a in audio_labels)
                + f"amix=inputs={len(audio_labels)}:duration=longest:normalize=0[aout]"
            )

    args = ["-hide_banner", "-loglevel", "error", "-y", *inputs,
            "-filter_complex", ";".join(filters), "-map", "[vout]"]
    if has_audio:
        args += ["-map", "[aout]", *preset.audio_args]
    else:
        args += ["-an"]
    args += [*preset.video_args, "-r", str(fps), *preset.container_args, str(output)]

    return Command(args=args, duration=timeline.duration, has_audio=has_audio)


def _fold_video_chain(
    filters: list[str],
    labels: list[str],
    durations: list[float],
    clips: list[Clip | None],
    *,
    fps: float,
) -> str:
    """Сворачивает сегменты видеоряда в один поток: обычная склейка (`concat`) между
    парами без перехода, `xfade` — там, где у клипа объявлен `transition_in` и он
    действительно примыкает (без разрыва) к предыдущему сегменту.

    `settb=1/{fps}` на каждом шаге — иначе `concat`/`xfade` отдают выход с чужой шкалой
    времени (например, 1/1000000 вместо 1/{fps}), и следующий `xfade` в цепочке падает
    с «input link timebase do not match» (ffmpeg 9.0.1, проверено на реальном экспорте).
    """
    vout = labels[0]
    accumulated = durations[0]

    for i in range(1, len(labels)):
        label, duration, clip = labels[i], durations[i], clips[i]
        prev_clip = clips[i - 1]
        transition = clip.transition_in if clip else None
        new_label = f"vx{i}"

        # Переход — это намеренное перекрытие ровно на transition.duration (ripple-shift
        # в ops.set_transition), а не точная стыковка «встык», как в обычном случае.
        if transition is not None and prev_clip is not None and abs(
            (prev_clip.end - clip.start) - transition.duration
        ) <= ADJACENCY_TOLERANCE:
            kind = TRANSITION_KINDS[transition.kind]
            offset = max(0.0, accumulated - transition.duration)
            filters.append(
                f"[{vout}][{label}]xfade=transition={kind}:duration={transition.duration:.4f}:"
                f"offset={offset:.4f},settb=1/{fps}[{new_label}]"
            )
            accumulated += duration - transition.duration
        else:
            if transition is not None:
                # Данные пришли не через ops.py (например, PUT /timeline напрямую) и не
                # согласованы — переход без реального примыкания рендерить нельзя, просто
                # клеим встык, чтобы не уронить экспорт.
                log.warning("Переход у клипа %s проигнорирован: клипы не встык", clip.id if clip else "?")
            filters.append(f"[{vout}][{label}]concat=n=2:v=1:a=0,settb=1/{fps}[{new_label}]")
            accumulated += duration

        vout = new_label

    return vout


def _apply_text_overlays(
    filters: list[str],
    inputs: list[str],
    vout: str,
    text_clips: list[Clip],
    *,
    width: int,
    height: int,
) -> str:
    """Накладывает текстовые слои поверх уже готового видеоряда (`overlay`, с fade по
    альфа-каналу) — drawtext в системном ffmpeg нет (нет libfreetype), поэтому текст
    рисуется в PNG через Pillow (см. `textlayer.py`) и подмешивается как отдельный вход."""
    for i, clip in enumerate(text_clips):
        png_path = Path(tempfile.gettempdir()) / f"editor_text_{clip.id}.png"
        render_text_png(
            clip.text or "", width=width, height=height,
            font_size=clip.font_size or 48, position=clip.position or "bottom",
            path=png_path,
        )
        inputs += ["-loop", "1", "-t", f"{clip.duration:.4f}", "-i", str(png_path)]
        index = _last_input(inputs)

        label = f"txt{i}"
        steps = ["format=yuva420p"]
        if clip.animation == "fade":
            fade = min(TEXT_FADE_SECONDS, clip.duration / 2)
            steps.append(f"fade=t=in:st=0:d={fade:.4f}:alpha=1")
            steps.append(f"fade=t=out:st={max(0.0, clip.duration - fade):.4f}:d={fade:.4f}:alpha=1")
        filters.append(f"[{index}:v]" + ",".join(steps) + f"[{label}]")

        # render_text_png уже кладёт текст в нужное место на холсте размера кадра —
        # накладываем во всю площадь, без своей x/y-арифметики поверх overlay.
        new_vout = f"vtxt{i}"
        filters.append(
            f"[{vout}][{label}]overlay=x=0:y=0:"
            f"enable='between(t,{clip.start:.4f},{clip.end:.4f})'[{new_vout}]"
        )
        vout = new_vout

    return vout


def _last_input(inputs: list[str]) -> int:
    """Индекс последнего добавленного входа."""
    return sum(1 for i, token in enumerate(inputs) if token == "-i") - 1


def _add_visual_input(inputs: list[str], source: MediaFile, clip: Clip) -> int:
    if clip.kind == "image":
        inputs += ["-loop", "1", "-t", f"{clip.duration:.4f}", "-i", source.path]
    else:
        # -ss перед -i: ffmpeg перематывает по ключевым кадрам и не декодирует лишнее.
        inputs += ["-ss", f"{clip.in_point:.4f}", "-t", f"{clip.source_duration:.4f}", "-i", source.path]
    return _last_input(inputs)


def _add_audio_input(inputs: list[str], source: MediaFile, clip: Clip) -> int:
    inputs += ["-ss", f"{clip.in_point:.4f}", "-t", f"{clip.source_duration:.4f}", "-i", source.path]
    return _last_input(inputs)


def _fade_steps(clip: Clip, kind: str) -> list[str]:
    """`fade`/`afade` считаются от начала клипа на его собственной (уже ускоренной) шкале —
    поэтому применяются после setpts/atempo, а не до."""
    steps = []
    if clip.fade_in > 1e-4:
        steps.append(f"{kind}=t=in:st=0:d={clip.fade_in:.4f}")
    if clip.fade_out > 1e-4:
        start = max(0.0, clip.duration - clip.fade_out)
        steps.append(f"{kind}=t=out:st={start:.4f}:d={clip.fade_out:.4f}")
    return steps


def _video_filter(index: int, clip: Clip, label: str, width: int, height: int, fps: float) -> str:
    """Приводит любой исходник к единому формату: иначе concat откажется склеивать."""
    steps = [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
        "setsar=1",
        f"fps={fps}",
        "format=yuv420p",
    ]
    if clip.kind != "image" and abs(clip.speed - 1.0) > 1e-6:
        steps.append(f"setpts=PTS/{clip.speed:.6f}")
    steps += _fade_steps(clip, "fade")
    return f"[{index}:v]" + ",".join(steps) + f"[{label}]"


def _audio_filter(filters: list[str], index: int, clip: Clip, label: str) -> str:
    steps = [*atempo_chain(clip.speed), *_fade_steps(clip, "afade")]
    delay_ms = int(round(clip.start * 1000))
    if delay_ms > 0:
        steps.append(f"adelay={delay_ms}:all=1")
    steps.append("aresample=async=1")
    filters.append(f"[{index}:a]" + ",".join(steps) + f"[{label}]")
    return label
