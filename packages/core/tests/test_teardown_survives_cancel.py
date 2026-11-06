"""Отмена прогона не должна пропускать уборку MCP.

``_aclose_mcp_and_release`` закрывает долгоживущие stdio-сессии, потом
stream-сессии, потом снимает владение. Первый шаг ждался напрямую, и
``CancelledError`` из него уносил управление мимо ВСЕГО остального:
stream-сессии не закрывались, владение не снималось.

Отмена приходит ровно тогда, когда прибираться нужнее всего — по стопу
пользователя или дедлайну, — и именно тогда браузер оставался жить.
``except Exception`` её не ловит: она ``BaseException``.

Ради этой проверки уборка и вынесена из ``finally`` отдельной функцией: внутри
``finally`` свойство сверялось бы только с копией блока в тесте, то есть никак.
"""

import asyncio

import pytest
from gc_core import agent_executor as ae


def _wire(monkeypatch, order: list, *, stdio_raises=None, stream_raises=None):
    class _StdioRegistry:
        @staticmethod
        def instance():
            return _StdioRegistry()

        async def aclose_all(self):
            order.append("stdio")
            if stdio_raises is not None:
                raise stdio_raises

    class _StreamManager:
        async def aclose_all(self, *, in_owner_task=False):
            assert in_owner_task
            order.append("stream")
            if stream_raises is not None:
                raise stream_raises

    monkeypatch.setattr(ae, "PersistentStdioRegistry", _StdioRegistry)
    monkeypatch.setattr(ae, "_STREAM_SESSION_MANAGER", _StreamManager())
    monkeypatch.setattr(ae, "reset_owner", lambda token: order.append("owner"))


async def test_all_steps_run_on_the_happy_path(monkeypatch):
    order: list = []
    _wire(monkeypatch, order)

    await ae._aclose_mcp_and_release(object())

    assert order == ["stdio", "stream", "owner"], order


async def test_cancel_in_the_first_step_does_not_skip_the_rest(monkeypatch):
    """Главное свойство: отмена на stdio не отменяет stream и снятие владения."""
    order: list = []
    _wire(monkeypatch, order, stdio_raises=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await ae._aclose_mcp_and_release(object())

    assert order == ["stdio", "stream", "owner"], (
        f"уборка оборвалась на отмене: {order}"
    )


async def test_cancel_in_the_second_step_still_releases_ownership(monkeypatch):
    order: list = []
    _wire(monkeypatch, order, stream_raises=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await ae._aclose_mcp_and_release(object())

    assert "owner" in order, f"владение не снято: {order}"


async def test_an_ordinary_error_does_not_stop_the_teardown(monkeypatch):
    """Обычная ошибка закрытия — тоже не повод бросать остальное."""
    order: list = []
    _wire(monkeypatch, order, stdio_raises=RuntimeError("сервер не отвечает"))

    await ae._aclose_mcp_and_release(object())  # без исключения наружу

    assert order == ["stdio", "stream", "owner"], order


async def test_cancellation_is_still_delivered_to_the_caller(monkeypatch):
    """Отмену не проглатываем: вызывающий обязан её увидеть — после уборки."""
    order: list = []
    _wire(monkeypatch, order, stdio_raises=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await ae._aclose_mcp_and_release(object())
