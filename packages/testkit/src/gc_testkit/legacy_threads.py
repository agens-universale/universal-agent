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


def install_legacy_threads(root: Path) -> Path:
    """Копия фикстур в ``root``; возвращает каталог тредов ``root/threads``.

    Рядом — ``root/workspace`` (рабочий каталог ходов) и
    ``root/responses-index`` (индекс chat), как в рабочей раскладке.
    """
    shutil.copytree(FIXTURE_ROOT, root, dirs_exist_ok=True)
    swaps = {
        "<THREADS_DIR>": _json_text(str(root / "threads")),
        "<WORKSPACE>": _json_text(str(root / "workspace")),
        "<ROOT>": _json_text(str(root)),
    }
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in _TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8")
            for placeholder, real in swaps.items():
                text = text.replace(placeholder, real)
            path.write_text(text, encoding="utf-8", newline="")
    return root / "threads"
