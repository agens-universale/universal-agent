"""Фикстуры тредов формата develop — эталон для хранилищ v2.

Файлы заморожены в ``tests/fixtures/legacy_threads/`` (см. README там же):
их создал код develop до изменений хранения, и на всех этапах переезда они
обязаны открываться и продолжаться. Абсолютные пути в них заменены
плейсхолдерами; :func:`install_legacy_threads` раскладывает копию и
подставляет настоящие.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from gc_testkit.paths import REPO

FIXTURE_DIR = REPO / "tests" / "fixtures" / "legacy_threads"
FIXTURE_ROOT = FIXTURE_DIR / "root"
_TEXT_SUFFIXES = {".json", ".jsonl"}


@dataclass(frozen=True)
class LegacyThread:
    """Тред фикстуры и то, что в нём лежит (посчитано по файлам)."""

    name: str
    system: str
    thread_id: str
    messages: int
    archives: int
    images: int
    subagent_events: int
    edits: int
    title: str | None
    first_user_text: str


def _load() -> dict[str, LegacyThread]:
    raw = json.loads((FIXTURE_DIR / "manifest.json").read_text("utf-8"))
    return {
        name: LegacyThread(name=name, **fields)
        for name, fields in raw["threads"].items()
    }


LEGACY_THREADS = _load()


def _json_text(value: str) -> str:
    """Строка в том виде, в каком она стоит внутри JSON-строки."""
    return json.dumps(value, ensure_ascii=False)[1:-1]


def _is_json_text(value: str) -> bool:
    if value[:1] not in ("{", "["):
        return False
    try:
        json.loads(value)
    except ValueError:
        return False
    return True


def _swap(value, swaps: dict[str, str]):
    if isinstance(value, dict):
        return {key: _swap(item, swaps) for key, item in value.items()}
    if isinstance(value, list):
        return [_swap(item, swaps) for item in value]
    if not isinstance(value, str) or not any(p in value for p in swaps):
        return value
    # Аргументы и вывод тула — JSON внутри JSON-строки: путь там стоит
    # в строке ещё одного уровня, и его слеши экранируются ещё раз.
    nested = _is_json_text(value)
    for placeholder, real in swaps.items():
        value = value.replace(
            placeholder, _json_text(real) if nested else real
        )
    return value


def substitute_paths(text: str, swaps: dict[str, str]) -> str:
    """Подставить настоящие пути вместо плейсхолдеров в ``.json``/``.jsonl``.

    Плейсхолдеры стоят только в однострочном JSON (строки истории и
    индексы). Строка с плейсхолдером разбирается и собирается обратно тем же
    ``json.dumps(..., ensure_ascii=False)``, которым её писал develop, —
    поэтому байты меняются только в подставленных путях.
    """
    out = []
    for line in text.split("\n"):
        if any(p in line for p in swaps):
            row = _swap(json.loads(line), swaps)
            line = json.dumps(row, ensure_ascii=False)
        out.append(line)
    return "\n".join(out)


def install_legacy_threads(root: Path) -> Path:
    """Копия фикстур в ``root``; возвращает каталог тредов ``root/threads``.

    Рядом — ``root/workspace`` (рабочий каталог ходов) и
    ``root/responses-index`` (индекс chat), как в рабочей раскладке.
    """
    shutil.copytree(FIXTURE_ROOT, root, dirs_exist_ok=True)
    swaps = {
        "<THREADS_DIR>": str(root / "threads"),
        "<WORKSPACE>": str(root / "workspace"),
        "<ROOT>": str(root),
    }
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in _TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8")
            path.write_text(
                substitute_paths(text, swaps), encoding="utf-8", newline=""
            )
    return root / "threads"
