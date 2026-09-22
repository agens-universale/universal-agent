"""Отмена ИЗНУТРИ сессии гасит остальные сессии владельца, а не только первую.

`aclose_all` перебрасывал `CancelledError` прямо из цикла, не разбирая, откуда
тот взялся. Если отменилась сама сессия (anyio cancel scope внутри её
`aclose`), закрытой оказывалась ровно одна, а остальные оставались в реестре и
ЖИВЫМИ. Для playwright это значит, что Chromium переживает агента.

Обратный случай — сняли НАС снаружи — разбирается ниже
(`test_an_outside_cancel_is_not_swallowed`) и в `test_session_teardown.py`:
отмена уходит наружу, но остаток перед этим дочищается в пределах ОБЩЕГО
бюджета. Оставить его «следующему сносу», как здесь считалось раньше, нельзя:
снос фильтрует по владельцу, а следующий владелец другой — у сервера контекст
новый на каждый вебхук, у CLI прогон один, переиспользует владельца только TUI.
Различаются эти два происхождения по счётчику `Task.cancelling()`.
"""
import asyncio
import time

import pytest
from gc_core.mcp_session_owner import reset_owner, set_owner
from gc_core.tools.connect_mcp import persistent_stdio as ps
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


class _Slow(_Session):
    async def aclose(self) -> None:
        await asyncio.sleep(0.05)
        self.closed = True


async def test_an_outside_cancel_is_not_swallowed():
    """Сняли НАС — отмена уходит наружу, но остаток перед этим дочищается.

    Происхождение отмены решает судьбу ОТМЕНЫ, а не остатка: внешнюю глотать
    нельзя, вызывающий обязан узнать, что его сняли. А вот бросать остаток
    нельзя тем более. Прежнее обоснование — «его закроет следующий снос или
    `close_everything`» — не выполняется ни в одной половине: снос фильтрует
    по владельцу, у сервера на каждый вебхук новый контекст и новый владелец,
    у CLI прогон один; а хук на выход ставился только на безвладельческом
    пути, то есть у владельческой сессии его не было вовсе.
    """
    sessions = [_Slow("a"), _Slow("b"), _Slow("c")]
    registry = _registry_with("owner-6", *sessions)

    token = set_owner("owner-6")
    try:
        task = asyncio.create_task(registry.aclose_all())
        await asyncio.sleep(0.02)              # дать начать первую
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        reset_owner(token)

    # Первая доигрывает под `shield` в фоне; смысл не в «успело до возврата», а
    # в «дошло до конца».
    for _ in range(50):
        if all(s.closed for s in sessions):
            break
        await asyncio.sleep(0.02)

    assert [s.name for s in sessions if not s.closed] == [], (
        "остаток брошен: его процессы переживут агента, а следующий снос придёт "
        "с другим владельцем и не увидит их"
    )
    assert registry._sessions == {}, f"остаток остался в реестре: {registry._sessions}"


async def test_the_drain_after_an_outside_cancel_is_bounded(monkeypatch):
    """Дочистка не имеет права держать снятый прогон дольше бюджета.

    Закрытие одной сессии стоит до 7 с (SIGTERM → таймаут → SIGKILL) плюс до
    10 с на ответ close-тула. Без общего потолка «дочистить остаток» означало
    бы держать уже снятый прогон десятки секунд — ровно то, чего прежний
    немедленный `raise` и избегал.
    """
    monkeypatch.setattr(ps, "_CANCEL_DRAIN_BUDGET_SEC", 0.1)

    class _Hangs(_Session):
        async def aclose(self) -> None:
            await asyncio.sleep(30)
            self.closed = True

    registry = _registry_with("owner-7", _Slow("slow"), _Hangs("hangs"))

    token = set_owner("owner-7")
    try:
        task = asyncio.create_task(registry.aclose_all())
        await asyncio.sleep(0.02)              # дать начать первую
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        spent = time.monotonic() - started
    finally:
        reset_owner(token)

    assert spent < 1.0, (
        f"отмена шла до вызывающего {spent:.2f} с при бюджете 0.1 — дочистка "
        f"не ограничена"
    )


async def test_what_the_budget_did_not_cover_stays_in_the_registry(monkeypatch):
    """Кончился бюджет — остаток остаётся В реестре, а не пропадает.

    Снять сессию с реестра и не закрыть — худший из исходов: она перестаёт
    быть видимой и хуку на выход процесса тоже. Поэтому бюджет проверяется ДО
    снятия сессии с реестра.
    """
    monkeypatch.setattr(ps, "_CANCEL_DRAIN_BUDGET_SEC", 0.0)

    registry = _registry_with("owner-8", _Slow("a"), _Slow("b"), _Slow("c"))

    token = set_owner("owner-8")
    try:
        task = asyncio.create_task(registry.aclose_all())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        reset_owner(token)

    assert len(registry._sessions) == 2, (
        f"сессии сняты с реестра, но не закрыты — их не увидит и хук на выход: "
        f"{registry._sessions}"
    )
