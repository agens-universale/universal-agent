"""Точка входа `gc-serve`.

Тонкая обёртка вокруг server.serve(). Нужна потому, что server.py импортирует
fastapi/uvicorn на уровне модуля: без extra [server] прямая точка входа
"server:serve" упала бы сырым ModuleNotFoundError ещё до вызова функции.

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

# Однострочно — как и сообщение TUI-обёртки: точка входа, упавшая из-за
# отсутствующего профиля, не должна разливаться на полэкрана. Команду собираем
# из PROFILE_SUBDIRS, чтобы подкаталог не разъезжался с реальной раскладкой.
_MISSING_SERVER_MSG = (
    "Серверный режим не установлен (нет пакета gc_server). Поставьте профиль: "
    + install_hint("gc-server")
)


# Что считаем «профиль server не установлен»: нет самого пакета gc_server (в
# headless-профиле он физически отсутствует) либо нет его сторонних
# зависимостей. Любой другой ImportError — настоящая поломка, её пробрасываем
# с трейсбеком, а не маскируем подсказкой про установку.
#
# Плоские имена `server`/`rest_api`/`server_cleanup_threads` мертвы с разбиения
# на пакеты: модули стали gc_server.*, и охранник перестал узнавать свой случай.
_SERVER_NAMES = {"gc_server", "fastapi", "uvicorn"}


def main() -> int:
    try:
        from gc_server.server import serve
    except ImportError as exc:
        if getattr(exc, "name", None) in _SERVER_NAMES:
            print(_MISSING_SERVER_MSG, file=sys.stderr)
            return 1
        raise

    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
