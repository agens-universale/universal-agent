"""Точка входа `gc-ua`. Живёт в ЯДРЕ, а не в пакете tui — и это принципиально.

Консольный шим, который генерирует установщик, содержит голый `from tui import
main` без try. Пока tui/ лежал в том же колесе, дружелюбное сообщение внутри
tui/__init__.py срабатывало: модуль импортировался, падал только textual. После
разделения на дистрибутивы при headless-профиле физически отсутствует сам пакет
tui — и весь тот текст становится недостижимым кодом, а пользователь получает
трейсбек от шима. Поэтому охранник обязан жить в дистрибутиве, который есть
всегда.
"""
from __future__ import annotations

import sys
from typing import Optional, Sequence

from gc_core.app_version import install_hint

# Однострочно: packages/tui/tests/test_runner_no_textual.py требует
# err.count("\n") <= 1. Команду собираем из PROFILE_SUBDIRS — строку с
# подкаталогом здесь уже один раз забыли обновить при переименовании каталога.
_MISSING_TUI_MSG = (
    "TUI не установлен (нет пакета gc_tui). Поставьте профиль с TUI: "
    + install_hint("gc-tui")
)

# Что считаем «профиль TUI не установлен»: нет самого пакета gc_tui либо нет его
# сторонних зависимостей. Любой другой ImportError — настоящая поломка, её
# прячем не мы: пробрасываем с трейсбеком.
#
# Имя именно `gc_tui`: до разбиения на пакеты пакет звался `tui`, и это
# множество осталось со старым именем — охранник переставал узнавать свой же
# случай и вместо однострочной подсказки показывал трейсбек (проверено вживую
# на профиле gc-cli 18.08.2026). Кодмод импортов такие строковые множества не
# видит, поэтому ниже стоит тест ровно на этот сценарий.
_TUI_NAMES = {"gc_tui", "textual", "rich", "pygments"}


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        import gc_tui as _tui
    except ImportError as exc:
        if getattr(exc, "name", None) in _TUI_NAMES:
            print(_MISSING_TUI_MSG, file=sys.stderr)
            return 1
        raise
    return _tui.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
