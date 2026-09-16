"""Отмена ИЗНУТРИ сессии гасит остальные сессии владельца, а не только первую.

`aclose_all` перебрасывал `CancelledError` прямо из цикла, не разбирая, откуда
тот взялся. Если отменилась сама сессия (anyio cancel scope внутри её
`aclose`), закрытой оказывалась ровно одна, а остальные оставались в реестре и
ЖИВЫМИ: `_arm_exit_hook` ставится только на безвладельческом пути, так что у
прогона со своим владельцем подстраховки на выходе может не быть вовсе. Для
playwright это значит, что Chromium переживает агента.

Обратный случай — сняли НАС снаружи — разбирается ниже
(`test_an_outside_cancel_is_not_swallowed`) и в `test_session_teardown.py`:
там остаток НАМЕРЕННО остаётся в реестре, а отмена пробрасывается сразу.
Различаются эти два происхождения по счётчику `Task.cancelling()`.
"""
import asyncio

import pytest

from gc_core.mcp_session_owner import reset_owner, set_owner
from gc_core.tools.connect_mcp.persistent_stdio import PersistentStdioRegistry


class _Session:
    def __init__(self, name: str, cancel: bool = False):
        self.name = name
        self.closed = False
        self._cancel = cancel

    async def aclose(self) -> None:
        self.closed = True
        if self._cancel:
            raise asyncio.CancelledError


def _registry_with(owner: str, *sessions: _Session) -> PersistentStdioRegistry:
    registry = PersistentStdioRegistry()
    for session in sessions:
        registry._sessions[(owner, session.name)] = session
    return registry


async def test_cancel_in_the_middle_still_closes_the_rest():
    sessions = [
        _Session("playwright", cancel=True),
        _Session("office"),
        _Session("ddg"),
    ]
    registry = _registry_with("owner-1", *sessions)

    token = set_owner("owner-1")
    try:
        with pytest.raises(asyncio.CancelledError):
            await registry.aclose_all()
    finally:
        reset_owner(token)

    assert [s.name for s in sessions if not s.closed] == [], (
        "после отмены остались незакрытые сессии — их процессы переживут агента"
    )
    assert registry._sessions == {}, "сессии остались в реестре"


async def test_cancel_is_not_swallowed():
    """Отмену копим, но отдаём: проглотить её — соврать планировщику."""
    registry = _registry_with("owner-2", _Session("only", cancel=True))

    token = set_owner("owner-2")
    try:
        with pytest.raises(asyncio.CancelledError):
            await registry.aclose_all()
    finally:
        reset_owner(token)


async def test_plain_failure_does_not_stop_the_teardown():
    """Обычная ошибка одной сессии не должна мешать остальным закрыться."""

    class _Broken(_Session):
        async def aclose(self) -> None:
            self.closed = True
            raise RuntimeError("stdin уже закрыт")

    broken, good = _Broken("broken"), _Session("good")
    registry = _registry_with("owner-3", broken, good)

    token = set_owner("owner-3")
    try:
        await registry.aclose_all()
    finally:
        reset_owner(token)

    assert broken.closed and good.closed


async def test_other_owners_are_left_alone():
    """Чужие сессии не трогаем — на общем loop живут соседние прогоны."""
    mine, alien = _Session("mine"), _Session("alien")
    registry = PersistentStdioRegistry()
    registry._sessions[("owner-4", "mine")] = mine
    registry._sessions[("owner-5", "alien")] = alien

    token = set_owner("owner-4")
    try:
        await registry.aclose_all()
    finally:
        reset_owner(token)

    assert mine.closed and not alien.closed
    assert list(registry._sessions) == [("owner-5", "alien")]


async def test_an_outside_cancel_is_not_swallowed():
    """Сняли НАС — останавливаемся, а остаток оставляем в реестре видимым.

    Происхождение отмены решает всё. Внутреннюю копим и дочищаем остальные;
    внешнюю глотать нельзя: доработать после неё значит держать снятый прогон
    ещё по 7 с на сессию (SIGTERM → таймаут → SIGKILL). Остаток при этом не
    теряется — он в реестре, его закроет следующий снос или
    `close_everything`.

    Без различителя (`Task.cancelling()`) этот тест краснеет: сессии `b` и `c`
    закрываются, и реестр пустеет.
    """
    class _Slow(_Session):
        async def aclose(self) -> None:
            await asyncio.sleep(0.05)
            self.closed = True

    registry = _registry_with("owner-6", _Slow("a"), _Slow("b"), _Slow("c"))

    token = set_owner("owner-6")
    try:
        task = asyncio.create_task(registry.aclose_all())
        await asyncio.sleep(0.02)              # дать начать первую
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        reset_owner(token)

    assert len(registry._sessions) == 2, f"остаток потерян: {registry._sessions}"
