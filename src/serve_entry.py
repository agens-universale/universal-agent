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
    "Серверный режим не установлен (нет fastapi/uvicorn). Поставьте профиль: "
    'uv tool install --force "gc-server @ '
    'git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packaging/server"'
)


# Что считаем «профиль [server] не установлен»: нет самого модуля server (после
# разделения на дистрибутивы он физически отсутствует в headless-профиле) либо
# нет его сторонних зависимостей. Любой другой ImportError — настоящая поломка,
# её пробрасываем с трейсбеком, а не маскируем подсказкой про установку.
_SERVER_NAMES = {"server", "server_cleanup_threads", "rest_api", "fastapi", "uvicorn"}


def main() -> int:
    try:
        from server import serve
    except ImportError as exc:
        if getattr(exc, "name", None) in _SERVER_NAMES:
            print(_MISSING_SERVER_MSG, file=sys.stderr)
            return 1
        raise

    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
