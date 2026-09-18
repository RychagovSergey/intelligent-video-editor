"""Правка `.env` из панели настроек (этап 8).

`.env` остаётся единственным источником правды — как для `MEDIA_ROOTS` (Р-11). Панель
не заводит второе хранилище настроек в базе, а переписывает значения в самом файле,
сохраняя комментарии и порядок строк: файл по-прежнему можно править руками.
"""
from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path

from .config import ENV_FILE, PROJECT_ROOT

EXAMPLE_FILE = PROJECT_ROOT / ".env.example"

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _quote(value: str) -> str:
    """Значения с пробелами и `#` python-dotenv читает только в кавычках."""
    if value == "" or not re.search(r"[\s#'\"\\]", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_env(text: str, updates: dict[str, str]) -> str:
    """Возвращает текст `.env` с подставленными значениями.

    Заменяется первая незакомментированная строка `KEY=`; ключей, которых в файле
    нет, дописываются в конец. Всё остальное — комментарии, пустые строки, порядок —
    остаётся как было.
    """
    pending = dict(updates)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = _LINE.match(line)
        if not match or match.group(1) not in pending:
            continue
        key = match.group(1)
        lines[i] = f"{key}={_quote(pending.pop(key))}"
    if pending:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# ---- Добавлено из панели настроек ----")
        lines.extend(f"{key}={_quote(value)}" for key, value in pending.items())
    return "\n".join(lines) + "\n"


def write_env(updates: dict[str, str], path: Path | None = None) -> Path:
    """Атомарно переписывает `.env`; при его отсутствии стартует с `.env.example`.

    Шаблон берётся ради комментариев: пользователь, открыв файл руками, увидит те же
    подсказки, что и при ручной установке. Права 600 — ключи API не должны читаться
    другими пользователями машины (Р-10).
    """
    path = path or ENV_FILE
    if path.exists():
        text = path.read_text(encoding="utf-8")
    elif EXAMPLE_FILE.exists():
        text = EXAMPLE_FILE.read_text(encoding="utf-8")
    else:
        text = ""

    rendered = render_env(text, updates)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".env_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        os.chmod(tmp_name, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path
