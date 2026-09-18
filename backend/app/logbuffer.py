"""Кольцевой буфер логов для нижней панели интерфейса (ТЗ п. 3.7).

Backend пишет в stdout uvicorn'а, которого пользователь десктопного приложения не
видит. Обработчик держит последние записи в памяти, а `/api/logs` отдаёт их с
порядковым номером — интерфейс дочитывает только новое. Логи доступа uvicorn сюда не
попадают: у его логгеров `propagate=False`, и это к лучшему — по одной строке на
каждый опрос состояния панель была бы бесполезна.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

CAPACITY = 1000


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = CAPACITY) -> None:
        super().__init__(level=logging.INFO)
        self._entries: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — битый формат не должен ронять логирование
            message = str(record.msg)
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
        with self._lock:
            self._seq += 1
            self._entries.append({
                "seq": self._seq,
                "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            })

    def since(self, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            fresh = [e for e in self._entries if e["seq"] > after]
        return fresh[-limit:]

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq


buffer = RingBufferHandler()


def install() -> None:
    root = logging.getLogger()
    if buffer not in root.handlers:
        root.addHandler(buffer)
