"""Общий запуск бинарников ffmpeg/ffprobe (ТЗ п. 4.1: пути берутся из настроек)."""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

from ..config import settings

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60.0


class BinaryMissing(RuntimeError):
    """Бинарник не найден ни в .env, ни в PATH."""


@dataclass
class Result:
    ok: bool
    stdout: str
    stderr: str
    code: int


def binary(name: str) -> str:
    path = settings.binary(name)
    if not path:
        raise BinaryMissing(f"{name} не найден: укажите путь в .env ({name.upper()}_PATH) или установите его")
    return path


def run(name: str, args: list[str], timeout: float = DEFAULT_TIMEOUT) -> Result:
    cmd = [binary(name), *args]
    log.debug("run: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return Result(False, "", f"Превышено время ожидания {name} ({timeout:.0f} с)", -1)
    return Result(proc.returncode == 0, proc.stdout, proc.stderr, proc.returncode)
