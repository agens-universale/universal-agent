"""Точка входа `gc-cli`. Живёт в ЯДРЕ — по той же причине, что tui_entry.

`uv tool install` кладёт в PATH ВСЕ пять команд дистрибутива, поэтому `gc-cli`
присутствует и в профилях `gc-tui` / `gc-server`, которые от `gc-cli` не
зависят и пакета `gc_cli` на диске не имеют. Без охранника такая команда даёт
сырой `ModuleNotFoundError: No module named 'gc_cli'` с трейсбеком.

Функциональности это не добавляет: headless-CLI профилем TUI не обещан.
Меняется только диагностика — вместо трейсбека одна строка с командой
установки нужного профиля.
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
