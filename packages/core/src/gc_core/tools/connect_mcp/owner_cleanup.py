"""Закрытие сессий владельца во всех трёх реестрах MCP.

Реестры процессные и ключуются владельцем: постоянный stdio
(``PersistentStdioRegistry``), менеджер сессий SDK (``stream_client``) и
идентификаторы сессий legacy-http (``client``). Здесь — единственное место,
которое знает, что сессия сервера живёт во всех трёх: закрыть сессию ключа
и закрыть все сессии владельца. Журнал процессов после закрытия чистит
вызывающий — сервис.
"""

from __future__ import annotations

from collections.abc import Callable

from gc_core.mcp_session_owner import use_owner
from gc_core.tools.connect_mcp import stream_client
from gc_core.tools.connect_mcp.client import (
    forget_owner_sessions,
    forget_session,
)
from gc_core.tools.connect_mcp.persistent_stdio import (
    PersistentStdioRegistry,
)


async def aclose_key_everywhere(
    owner: str,
    session_key: str,
    server_key: str,
    *,
    legacy_is_ours: Callable[[], bool],
) -> list[Exception]:
    """Сессия ``session_key`` владельца ``owner`` — в реестре постоянного
    stdio и в менеджере SDK; затем идентификатор legacy-http сервера
    ``server_key``, если ``legacy_is_ours()`` (он ключуется именем сервера
    и мог уже перейти к новой записи). Исключения закрытий не прерывают
    остальные шаги и возвращаются списком."""
    errors: list[Exception] = []
    registry = PersistentStdioRegistry.instance()
    # Через модуль, а не по имени: тесты подменяют `_SESSION_MANAGER`.
    manager = stream_client._SESSION_MANAGER
    with use_owner(owner):
        for close in (registry.aclose_one, manager.ainvalidate):
            try:
                await close(session_key)
            except Exception as exc:
                errors.append(exc)
        if legacy_is_ours():
            forget_session(server_key)
    return errors


async def aclose_owner_everywhere(owner: str) -> None:
    """Все сессии владельца ``owner`` во всех трёх реестрах; шаги не
    зависят друг от друга — прерывание одного не отменяет остальных."""
    with use_owner(owner):
        try:
            await PersistentStdioRegistry.instance().aclose_all()
        finally:
            try:
                await stream_client._SESSION_MANAGER.aclose_all(
                    in_owner_task=False
                )
            finally:
                forget_owner_sessions()


__all__ = ["aclose_key_everywhere", "aclose_owner_everywhere"]
