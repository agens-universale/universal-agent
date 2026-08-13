"""Точка входа `gc-serve`.

Тонкая обёртка вокруг server.serve(). Нужна потому, что server.py импортирует
fastapi/uvicorn на уровне модуля: без extra [server] прямая точка входа
"server:serve" упала бы сырым ModuleNotFoundError ещё до вызова функции.

uv tool install кладёт в PATH ВСЕ скрипты дистрибутива независимо от extras,
поэтому gc-serve присутствует и в headless-профиле и обязан объяснять, чего
не хватает, а не показывать трейсбек. Тот же приём, что в tui/__init__.py.
"""
from __future__ import annotations

import sys

# Однострочно — как и сообщение TUI-обёртки (tui/__init__.py): точка входа,
# упавшая из-за отсутствующего extra, не должна разливаться на полэкрана.
_MISSING_SERVER_MSG = (
    "Режим server требует пакеты fastapi/uvicorn (extra 'server'). Переустановите: "
    'uv tool install --force "gc-ua[server] @ '
    'git+https://gitverse.ru/gigacode-ai/universal-agent"'
)


def main() -> int:
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        print(_MISSING_SERVER_MSG, file=sys.stderr)
        return 1

    from server import serve

    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
