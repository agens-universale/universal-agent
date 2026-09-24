"""Сторожа находок ревью ветки feat/async-only (средний уровень).

Один файл на несколько находок сознательно: каждая — двух-трёх строчная
проверка одного свойства, и раскладывать их по отдельным файлам значило бы
плодить преамбулы. Блокеры (F1–F5) лежат отдельно, у каждого свой файл со
своим стендом.
"""

import asyncio

import pytest

# --- F8: ключ кэша MCP-сессий -------------------------------------------


def test_forget_session_actually_removes_the_entry():
    """`forget_session` обязан попадать в ключ, которым сессию клали.

    Кэш ключуется парой ``(владелец, сервер)`` с тех пор, как сессии развели
    по владельцам. Шесть мест в `client_port` продолжали делать
    `pop(server_name)` — промах по ключу, молча: ветка «сессия истекла →
    переинициализировать» доставала из кэша тот же протухший id и повторяла
    неудачный запрос.
    """
    from gc_core.mcp_session_owner import current_owner
    from gc_core.tools.connect_mcp.client import _MCP_SESSIONS, forget_session

    key = (current_owner(), "srv")
    _MCP_SESSIONS[key] = "sid-stale"
    try:
        forget_session("srv")

        assert key not in _MCP_SESSIONS, (
            f"запись осталась: {_MCP_SESSIONS} — снятие снова промахивается "
            f"мимо ключа, и re-init получит тот же протухший id"
        )
    finally:
        _MCP_SESSIONS.pop(key, None)


def test_client_port_no_longer_pops_by_bare_server_name():
    """И сам промах: голого `pop(server_name)` в порту быть не должно."""
    import pathlib

    from gc_core.tools.connect_mcp import client_port

    src = pathlib.Path(client_port.__file__).read_text(encoding="utf-8")

    assert "_MCP_SESSIONS.pop(" not in src, (
        "в client_port вернулось снятие сессии мимо ключа (owner, server)"
    )


# --- F10: редиректы -----------------------------------------------------


async def test_async_client_follows_redirects():
    """Паритет с синхронным клиентом и с `requests.Session`, откуда переезжали.

    Корпоративные шлюзы отвечают 307/308 на нормализацию http→https или
    завершающего слэша у `/v1/chat/completions`. У httpx дефолт обратный, и
    `raise_for_status()` бросает уже на 3xx; ретрай это не спасал — 307 не 429
    и не 5xx.
    """
    from gc_core.utils.async_http import aclose_clients, get_async_client

    try:
        assert get_async_client().follow_redirects is True
    finally:
        await aclose_clients()


def test_both_transports_agree_on_redirects():
    """Два клиента не должны расходиться в этом свойстве."""
    from gc_core.utils.http_session import NO_NETRC_SESSION

    assert NO_NETRC_SESSION.follow_redirects is True


# --- F12: тела гвардов не делят пул с MCP -------------------------------


def test_guard_bodies_have_their_own_slots():
    """Слоты гвардов и слоты MCP — РАЗНЫЕ объекты.

    Тело модерации в режиме `ask` держит слот всё время, пока человек думает.
    Пока пул был общий, несколько агентов на одном loop с ожидающими
    подтверждениями выбирали восемь слотов и останавливали MCP-вызовы соседей.
    """
    from gc_core.security import dual_wrapper
    from gc_core.tools.connect_mcp import abandonable

    assert dual_wrapper._GUARD_SLOTS is not abandonable._slots


async def test_a_busy_guard_pool_does_not_block_mcp():
    """Свойство, а не только раздельность объектов: MCP проходит сквозь.

    Забиваем ВСЕ слоты гвардов и проверяем, что вызов на пуле MCP всё равно
    исполняется. На общем пуле он бы ждал освобождения.
    """
    from gc_core.security import dual_wrapper
    from gc_core.tools.connect_mcp.abandonable import run_abandonable

    held = []
    for _ in range(32):
        dual_wrapper._GUARD_SLOTS.acquire()
        held.append(1)
    try:
        got = await asyncio.wait_for(run_abandonable(lambda: "mcp прошёл"), 5)
        assert got == "mcp прошёл"
    finally:
        for _ in held:
            dual_wrapper._GUARD_SLOTS.release()


# --- F9: отмена stream() ------------------------------------------------


class _SlowExecutor:
    """Ход, который идёт дольше, чем готов ждать потребитель."""

    def __init__(self):
        self.stopped = False

    async def arun(self, *_a, **_kw):
        await asyncio.sleep(3)
        return "поздний ответ"

    def stop_run(self):
        self.stopped = True


async def test_stream_cancellation_is_not_deferred_to_the_end_of_the_turn():
    """Отмена потребителя обязана прекратить ход, а не ждать его конца.

    Отмена и брошенный итератор — разные вещи. Пока обе ветки шли через один
    `await task`, `wait_for(..., 0.2)` возвращал `TimeoutError` только через
    все три секунды прогона, а LLM-вызовы и тулы продолжали идти.
    """
    from gc_core.aio.agent import AsyncAgent

    executor = _SlowExecutor()
    agent = AsyncAgent(executor, set())

    async def _consume():
        async for _ in agent.stream("вопрос"):
            pass

    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await asyncio.wait_for(_consume(), 0.2)
    spent = loop.time() - started

    assert spent < 1.5, f"отмена ждала конца хода: {spent:.2f}с"
    assert executor.stopped, "ход не попросили остановиться"


async def test_breaking_out_of_the_iterator_still_waits_for_the_turn():
    """Обратная половина контракта: `break` ход по-прежнему доигрывает.

    Без неё «починка» отмены превратилась бы в осиротевший прогон, чьё
    исключение всплывает ниоткуда.
    """
    from gc_core.aio.agent import AsyncAgent

    class _Quick:
        def __init__(self):
            self.finished = False

        async def arun(self, *_a, **_kw):
            await asyncio.sleep(0.05)
            self.finished = True
            return "готово"

        def stop_run(self):
            raise AssertionError("break не должен останавливать ход")

    executor = _Quick()
    agent = AsyncAgent(executor, set())

    async for _ in agent.stream("вопрос"):
        break

    assert executor.finished, "ход не доигран"
    assert agent.last_answer == "готово"
