"""Склонения числительных UI — один модуль на всю кодовую базу.

Формы проверяются на 1/2/5/11/21/68/111: именно на этих числах копии словаря
расходились («… ещё 2 строк» ловили на живой приёмке волны 2).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from gc_tui.services.plural import (
    errors_word,
    files_word,
    files_word_genitive,
    lines_word,
    plural_form,
    refusals_word,
)
from gc_testkit.paths import module_dir

_SRC_TUI = module_dir("tui")
_PLURAL_MODULE = _SRC_TUI / "services" / "plural.py"
# Опознавательный знак самого правила: полоса «11–14» (в старых копиях «11–19»),
# по которой отличают форму множественного числа от формы по последней цифре.
# Именно её и переписывали каждый раз заново, как бы ни звалась функция.
_PLURAL_BAND = re.compile(r"11\s*<=[^<]*<=\s*1[49]")


@pytest.mark.parametrize(
    "n,word",
    [
        (0, "файлов"),
        (1, "файл"),
        (2, "файла"),
        (5, "файлов"),
        (11, "файлов"),
        (21, "файл"),
        (68, "файлов"),
        (111, "файлов"),
    ],
)
def test_files_word(n, word):
    assert files_word(n) == word


@pytest.mark.parametrize(
    "n,word",
    [
        (0, "файлов"),
        (1, "файла"),
        (2, "файлов"),
        (5, "файлов"),
        (11, "файлов"),
        (21, "файла"),
        (68, "файлов"),
        (111, "файлов"),
    ],
)
def test_files_word_genitive(n, word):
    """Родительный падеж после предлога («у 2 файлов», не «у 2 файла»): в
    отличие от счётной `files_word`, 2–4 здесь не отличаются от 5+ — обе
    ведут множественное число, различает только «оканчивается на 1» (кроме
    11)."""
    assert files_word_genitive(n) == word


@pytest.mark.parametrize(
    "n,word",
    [
        (0, "строк"),
        (1, "строка"),
        (2, "строки"),
        (5, "строк"),
        (11, "строк"),
        (21, "строка"),
        (68, "строк"),
        (111, "строк"),
    ],
)
def test_lines_word(n, word):
    assert lines_word(n) == word


@pytest.mark.parametrize(
    "n,word",
    [(1, "ошибка"), (2, "ошибки"), (5, "ошибок"), (11, "ошибок")],
)
def test_errors_word(n, word):
    assert errors_word(n) == word


@pytest.mark.parametrize(
    "n,word",
    [(1, "отказ"), (2, "отказа"), (5, "отказов"), (21, "отказ")],
)
def test_refusals_word(n, word):
    assert refusals_word(n) == word


@pytest.mark.parametrize(
    "n,word",
    [
        (1, "обновление"),
        (2, "обновления"),
        (5, "обновлений"),
        (11, "обновлений"),
        (21, "обновление"),
    ],
)
def test_plural_form_serves_a_word_of_its_own(n, word):
    """Редкое слово берёт правило напрямую — своей копии оно не заводит."""
    assert plural_form(n, "обновление", "обновления", "обновлений") == word


def test_no_private_plural_copies_left():
    """Приватных копий склонения в `src/tui` не осталось.

    Страж именно на приватные имена: `_plural_lines` в `paste_chips`,
    `_files_word` в индикаторе и `_ru_plural` в секции MCP — те самые копии,
    ради которых заведён модуль. Импорт приватного имени из чужого модуля был
    бы вторым источником форм.
    """
    offenders = sorted(
        str(path.relative_to(_SRC_TUI))
        for path in _SRC_TUI.rglob("*.py")
        if any(
            name in path.read_text(encoding="utf-8")
            for name in ("_plural_lines", "_files_word", "_ru_plural")
        )
    )
    assert offenders == []


def test_no_second_implementation_of_the_rule():
    """Четвёртой копии не будет и под другим именем.

    Страж на имена ловит только уже известные три; сам алгоритм узнаётся по
    полосе «11–14» — её переписывали каждый раз заново, как бы ни звалась
    функция вокруг. Нужна своя пара слов — есть `plural_form`.
    """
    offenders = sorted(
        str(path.relative_to(_SRC_TUI))
        for path in _SRC_TUI.rglob("*.py")
        if path != _PLURAL_MODULE
        and _PLURAL_BAND.search(path.read_text(encoding="utf-8"))
    )
    assert offenders == []
