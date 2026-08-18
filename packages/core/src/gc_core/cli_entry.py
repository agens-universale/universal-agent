"""Точка входа `gc-cli`. Живёт в ЯДРЕ — по той же причине, что tui_entry.

Зачем охранник, если профиль объявляет только свою команду.
С 18.08.2026 имена команд разведены: лист объявляет свою, мета — все. В здоровой
установке ветка отказа недостижима. Она остаётся страховкой на битое окружение
(пакет снесли из venv вручную, установка оборвалась на середине) — там дешевле
одна строка с командой установки, чем трейсбек. Поведение закреплено тестом
packages/core/tests/test_missing_profile_guards.py.
"""
from __future__ import annotations

import sys

from gc_core.app_version import install_hint

# Однострочно — как у соседних шимов: точка входа, упавшая из-за отсутствующего
# профиля, не должна разливаться на полэкрана.
_MISSING_CLI_MSG = (
    "Headless-CLI не установлен (нет пакета gc_cli). Поставьте профиль: "
    + install_hint("gc-cli")
)

# Что считаем «профиль CLI не установлен»: нет самого пакета gc_cli. Любой
# другой ImportError — настоящая поломка, её пробрасываем с трейсбеком.
_CLI_NAMES = {"gc_cli"}


def main() -> int:
    try:
        from gc_cli.cli import main as _main
    except ImportError as exc:
        if getattr(exc, "name", None) in _CLI_NAMES:
            print(_MISSING_CLI_MSG, file=sys.stderr)
            return 1
        raise
    return _main()


if __name__ == "__main__":
    raise SystemExit(main())
