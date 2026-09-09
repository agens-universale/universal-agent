"""Кто владеет живой MCP-сессией.

Задача. Долгоживущие MCP-сессии кэшируются по имени сервера, и до этого модуля
в ключе не было никакого «чей». На один процесс и один event loop это работало,
пока агент был один. Как только их стало несколько — сломалось:

``AgentExecutor.arun`` в своём ``finally`` закрывает сессии. Раз ключ общий,
ПЕРВЫЙ доигравший агент сносит ЖИВЫЕ сессии всех остальных. У stdio симптом
мягкий — процесс убит, следующий вызов поднимает новый (лишние секунды и
потерянное состояние браузера). У ``stream_*`` — падение: сессии MCP SDK
построены на anyio-scope'ах, вошли в них задачи-владельцы, а закрывают их из
чужой задачи, и anyio отменяет не ту::

    CancelledError: Cancelled via cancel scope … by <Task pending name='Task-5'>

Решение. Токен владельца в ``ContextVar``. Его ставит ``create_run_context``
(чтобы сессии, поднятые дискавери, принадлежали этому же контексту) и заново
входит в него ``arun``. Ключи всех трёх реестров начинаются с этого токена, а
teardown закрывает только своё.

Почему именно ``ContextVar``, а не аргумент. MCP-клиент про ``RunContext`` не
знает и знать не должен: ``MCPToolWrapper`` собирается фабрикой из одного лишь
``config_dict``. Протащить владельца параметром — значит поменять сигнатуры от
``build_tools_data`` до каждого транспорта. ``ContextVar`` доезжает сам:
``asyncio.to_thread`` копирует контекст, порождённые задачи наследуют его,
а ``run_abandonable`` копирует явно.

Почему модуль лежит в корне ``gc_core``, а не рядом с MCP-клиентом. ``_OWNER``
обязан быть ОДНИМ на процесс, а ``tests/test_inject_tool_fields.py`` автоused-
фикстурой выкидывает все ``gc_core.tools.*`` из ``sys.modules`` до и после
каждого своего теста. Модуль под ``tools`` пересоздавался бы вместе с ними —
и половина кода читала бы уже другой ``ContextVar``, из-за чего владельцы
молча переставали различаться. Поймано ровно так: гейт зелёный в одиночку и
красный в общем прогоне.

Токен ``None`` — легальное состояние: прямые вызовы ``apply_tool`` из тестов и
``gc-mcp`` идут вне всякого прогона. Такие сессии складываются в свою корзину.

Закрывает их ``atexit``-хук, поставленный при первой же безвладельческой
сессии. Раньше здесь стояло обещание «закрываются явным
``close_everything()``», и оно не выполнялось: вызова не было НИ В ОДНОМ
пакете. Сервер вдобавок стартует с ``start_new_session=True`` и SIGHUP не
получит, так что рассчитывать на сигнал тоже нельзя. Хук — не идеал (при
SIGKILL он не отработает), но это разница между «обычно закрываем» и
«не закрываем никогда».
"""
from __future__ import annotations

import contextvars
import itertools
from contextlib import contextmanager
from typing import Iterator, Optional

#: Владелец живых MCP-сессий в текущем контексте исполнения.
_OWNER: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "gc_mcp_session_owner", default=None
)

#: Счётчик вместо uuid: токены нужны только для различения внутри процесса,
#: а монотонный счётчик читается в логах и тестах куда лучше случайной строки.
_counter = itertools.count(1)


def new_owner_token(prefix: str = "ctx") -> str:
    """Свежий токен владельца. Уникален в пределах процесса."""
    return f"{prefix}-{next(_counter)}"


def current_owner() -> Optional[str]:
    """Владелец текущего контекста исполнения либо ``None``."""
    return _OWNER.get()


@contextmanager
def use_owner(token: Optional[str]) -> Iterator[Optional[str]]:
    """Войти во владение на время блока.

    ``None`` тоже входит явно: иначе вложенный прогон унаследовал бы владельца
    родителя и закрыл бы его сессии на своём выходе.
    """
    reset = _OWNER.set(token)
    try:
        yield token
    finally:
        _OWNER.reset(reset)


def set_owner(token: Optional[str]) -> contextvars.Token:
    """Войти во владение без ``with``-блока.

    Нужно там, где блок не обернуть без переиндентации всего тела — например
    в ``AgentExecutor.arun``, где владелец должен жить ровно от начала прогона
    до конца ``finally`` с закрытием сессий. Парный ``reset_owner`` обязателен.
    """
    return _OWNER.set(token)


def reset_owner(token: contextvars.Token) -> None:
    """Выйти из владения, полученного через ``set_owner``."""
    _OWNER.reset(token)


def copy_context_for_thread() -> contextvars.Context:
    """Снимок контекста для потока, запущенного в обход ``asyncio.to_thread``.

    ``to_thread`` копирует контекст сам, а голый ``threading.Thread`` — нет.
    Дискавери ходит через ``run_abandonable`` с обычным потоком, и без этого
    снимка сессия, поднятая там, оказалась бы ничьей.
    """
    return contextvars.copy_context()


__all__ = [
    "copy_context_for_thread",
    "current_owner",
    "new_owner_token",
    "reset_owner",
    "set_owner",
    "use_owner",
]
