"""Запуск ffmpeg с прогрессом и отменой (ТЗ пп. 3.4, 3.6)."""
from __future__ import annotations

import logging
import re
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from .compile import Command
from .runner import binary

log = logging.getLogger(__name__)

#: ffmpeg с `-progress pipe:1` печатает пары ключ=значение; нас интересует время.
_TIME_RE = re.compile(r"out_time_ms=(\d+)")

ProgressCb = Callable[[float, float], None]   # (готово секунд, всего секунд)


class RenderCancelled(RuntimeError):
    pass


class RenderFailed(RuntimeError):
    pass


def render(
    command: Command,
    *,
    on_progress: ProgressCb | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """Выполняет собранную команду, сообщая прогресс по времени вывода."""
    args = [binary("ffmpeg"), *command.args, "-progress", "pipe:1", "-nostats"]
    log.info("Рендер: %s сек", command.duration)

    process = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )
    stderr_tail: list[str] = []

    def drain_stderr() -> None:
        for line in process.stderr or []:
            stderr_tail.append(line.rstrip())
            del stderr_tail[:-40]

    watcher = threading.Thread(target=drain_stderr, daemon=True)
    watcher.start()

    try:
        for line in process.stdout or []:
            if should_stop and should_stop():
                process.kill()
                raise RenderCancelled("Рендер остановлен")
            match = _TIME_RE.search(line)
            if match and on_progress:
                on_progress(int(match.group(1)) / 1_000_000, command.duration)
    finally:
        process.stdout and process.stdout.close()
        code = process.wait()
        watcher.join(timeout=1)

    if code != 0:
        tail = "\n".join(stderr_tail[-8:]) or f"ffmpeg завершился с кодом {code}"
        raise RenderFailed(tail)

    if on_progress:
        on_progress(command.duration, command.duration)


def play(path: Path, *, title: str = "Предпросмотр") -> subprocess.Popen:
    """Открывает готовый файл в ffplay отдельным окном (ТЗ п. 3.4)."""
    args = [
        binary("ffplay"),
        "-hide_banner", "-loglevel", "error",
        "-autoexit",
        "-window_title", title,
        str(path),
    ]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
