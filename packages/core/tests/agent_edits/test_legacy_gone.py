"""Стражи смерти legacy-модели правок (Р14): пакет, слова, документация.

Экран правок построен на журнале `agent_edits`; approve/pending-механики и
пакета `diff_tui` больше нет. Три утверждения, которые обязаны держаться сами:
кода нет, слов нет, документация не описывает то, чего нет.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from gc_testkit.paths import REPO, module_dir

_ROOT = REPO
_TUI_SRC = module_dir("tui")

# Слова умершей модели, у которых в продукте нет второго смысла. Голое
# «pending» сюда не годится: в `src/tui` оно законно живёт у неотправленных
# реплик пользователя (`pending_user_messages`), а «принят»/«принять» —
# в настройках («токен принят») и в экране правок (волна 4 узаконила
# принятие как действие над точкой отсчёта, §7 спеки). Экран правок и его
# сервис закрыты отдельным, более строгим стражем —
# `tests/tui/test_diff_screen.py::test_no_approve_wording_anywhere`.
_DEAD_UI_WORDS = (
    "approve",
    "заапрув",
    "pending-дифф",
    "pending-правк",
)

# Документация — текст для пользователя. «pending»/«approve» описывали
# только умершую механику, `.gc/diffs` — снесённый стор; голое «приня» здесь
# больше не ловим — волна 4 узаконила «принять»/«принято» в новом смысле
# (§7 спеки), и в доках это слово теперь законно описывает саму функцию.
_DEAD_DOC_WORDS = ("pending", "approve", "заапрув", ".gc/diffs")

_DOCS = ("docs/tui.md", "readme.md")


def test_diff_tui_package_no_longer_exists():
    """Пакета нет ни целиком, ни модулями — включая namespace-каталог."""
    assert not (module_dir("tui") / "diff_tui").exists()
    for name in (
        "diff_tui",
        "diff_tui.service",
        "diff_tui.store",
        "diff_tui.feedback",
        "diff_tui.visual",
        "diff_tui.touched",
        "diff_tui.git_ops",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(name)


def test_no_approve_wording_left_in_ui_sources():
    """Р1: слов умершей approve-модели в исходниках TUI не осталось."""
    offenders = []
    for path in _TUI_SRC.rglob("*.py"):
        low = path.read_text(encoding="utf-8").lower()
        for dead in _DEAD_UI_WORDS:
            if dead in low:
                offenders.append(f"{path.relative_to(_ROOT)}: {dead}")
    assert offenders == []


def test_docs_do_not_describe_dead_model():
    """Документация не описывает approve/pending и снесённое хранилище."""
    offenders = []
    for rel in _DOCS:
        text = (_ROOT / rel).read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            low = line.lower()
            for dead in _DEAD_DOC_WORDS:
                if dead in low:
                    offenders.append(f"{rel}:{number}: {dead}")
    assert offenders == []
