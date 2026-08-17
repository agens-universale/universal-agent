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

# Однострочно: tests/tui/test_runner_no_textual.py требует err.count("\n") <= 1.
# `#subdirectory=` обязателен: uv читает из git только корневой pyproject.toml,
# а корень — это полный профиль gc-ua.
_MISSING_TUI_MSG = (
    "TUI не установлен (нет пакета textual). Поставьте профиль с TUI: "
    'uv tool install --force "gc-tui @ '
    'git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packaging/tui"'
)

# Что считаем «профиль [tui] не установлен»: нет самого пакета tui либо нет его
# сторонних зависимостей. Любой другой ImportError — настоящая поломка, её
# прячем не мы: пробрасываем с трейсбеком.
_TUI_NAMES = {"tui", "textual", "rich", "pygments"}


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        import tui as _tui
    except ImportError as exc:
        if getattr(exc, "name", None) in _TUI_NAMES:
            print(_MISSING_TUI_MSG, file=sys.stderr)
            return 1
        raise
    return _tui.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
