"""Приёмка MCP: три случая, на которых мы обожглись, закреплены гейтами.

Каждый из трёх стоил половины дня и ни один не ловился юнит-тестом на моках —
именно поэтому они здесь, а не в общем наборе ``test_mcp_*``.

1. **Объявленные, но не реализованные capabilities.** ``roots`` и ``sampling``
   в ``initialize`` заставляют сервер ждать свой таймаут. Замерено на живом
   ``@playwright/mcp``: ``{}`` → 1,1 с, ``roots`` → 61,2 с, ``roots`` +
   ``sampling`` → 64,2 с. До того как причина нашлась, были проверены и
   отброшены три неверные гипотезы: недоступность сети,
   ``--timeout-navigation`` и параметры spawn.

   ``test_mcp_declared_capabilities`` следит за СОСТАВОМ объявленного. Здесь —
   за последствием: сервер, который ждёт обещанного, не должен нас задержать,
   потому что мы ничего не обещали.

2. **Владение сессией.** Закрытие из чужой задачи рвёт не тот cancel scope
   anyio: параллельный агент в этот момент внутри вызова тула. Понадобился
   ``ContextVar``-токен владельца.

   ``test_mcp_session_owner`` проверяет сам реестр. Здесь — что закрытие
   ДЕЙСТВИТЕЛЬНО не трогает чужую живую сессию.

3. **Раздельность сессий по транспортам.** ``persistent: false`` — процесс на
   каждый вызов; ``stream_http`` + ``persistent: false`` — своя сессия на
   агента.

Живая часть (реальный сервер, реальный браузер, замер handshake) остаётся в
[ручных тестах](../../../docs/manual-tests.md) — сюда попадает то, что
воспроизводится в процессе.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from gc_core.mcp_session_owner import new_owner_token, use_owner
from gc_core.tools.connect_mcp.transport import _INIT_PAYLOAD, aget_transport


# --- M-1/M-2: не обещаем того, чего не делаем ------------------------------

def test_initialize_promises_nothing_we_cannot_serve():
    """В ``initialize`` не объявлено ни одной способности.

    Регресс-гейт на 61-секундный инцидент. Пустой словарь — не небрежность:
    любая объявленная способность означает, что сервер вправе прислать нам
    встречный запрос и ЖДАТЬ ответа, а обработчика входящих запросов у нас нет.
    """
    params = _INIT_PAYLOAD["params"]
    assert params.get("capabilities") == {}, (
        f"объявлены способности {params.get('capabilities')!r}: сервер будет ждать "
        "встречного запроса, которого мы не обслужим, и выждет свой таймаут"
    )


#: Фальшивый сервер, ждущий обещанного. Механика — как у живого
#: ``@playwright/mcp``, только ожидание укорочено.
_FAKE_SERVER = Path(__file__).parent / "fixtures" / "stalling_mcp_server.py"
_STALL_SECONDS = 2.0


def _stdio_cfg(**env_extra) -> dict:
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(_FAKE_SERVER)],
        "env": {"STALL_SECONDS": str(_STALL_SECONDS), **env_extra},
    }


async def test_handshake_is_fast_against_a_server_that_waits():
    """НАСТОЯЩИЙ РЕГРЕСС-ГЕЙТ на 61-секундный инцидент.

    Наш клиент идёт к серверу, который ждёт от клиента ответа на встречный
    запрос — но только если клиент что-то пообещал. Мы не обещаем ничего,
    поэтому handshake проходит мгновенно.

    Проверяется не состав ``capabilities`` (это делает соседний тест), а
    ПОСЛЕДСТВИЕ: время. Тест покраснеет и в том случае, если способность
    появится не в ``_INIT_PAYLOAD``, а где-то ещё по пути.
    """
    request = aget_transport(_stdio_cfg())

    started = time.monotonic()
    _headers, body = await request("", {}, dict(_INIT_PAYLOAD), 30.0)
    spent = time.monotonic() - started

    assert body.get("result"), f"сервер не ответил на initialize: {body!r}"
    assert spent < _STALL_SECONDS, (
        f"handshake занял {spent:.2f}с при ожидании сервера {_STALL_SECONDS}с — "
        "значит мы объявили способность, которую не обслуживаем, и сервер "
        "выждал свой таймаут. Замер на живом сервере: 1,1с против 61,2с."
    )


async def test_the_gate_would_catch_a_regression(monkeypatch):
    """Контроль: гейт выше действительно ловит, а не зелёный по случайности.

    Объявляем способность, которой не обслуживаем, — и тот же сервер начинает
    ждать. Без этой проверки предыдущий тест мог бы проходить потому, что
    фальшивый сервер вообще не умеет ждать.
    """
    from gc_core.tools.connect_mcp import transport as transport_mod

    payload = json.loads(json.dumps(_INIT_PAYLOAD))
    payload["params"]["capabilities"] = {"roots": {"listChanged": True}}
    monkeypatch.setitem(transport_mod._INIT_PAYLOAD["params"],
                        "capabilities", payload["params"]["capabilities"])

    request = aget_transport(_stdio_cfg())
    started = time.monotonic()
    _headers, body = await request("", {}, payload, 30.0)
    spent = time.monotonic() - started

    assert body.get("result"), "сервер всё же обязан ответить, но позже"
    assert spent >= _STALL_SECONDS * 0.8, (
        f"объявленная способность НЕ вызвала ожидания ({spent:.2f}с) — "
        "фальшивый сервер не воспроизводит механику, и гейт выше ничего не стоит"
    )


def test_protocol_version_is_declared():
    """Версия протокола на месте: без неё сервер вправе отказать."""
    assert _INIT_PAYLOAD["params"].get("protocolVersion")


async def test_unsolicited_notification_is_not_mistaken_for_the_answer():
    """Уведомление сервера не принимается за ответ.

    Найдено этой же приёмкой. Клиент читал ОДНУ строку и считал её ответом —
    что бы в ней ни лежало, — хотя докстрока рядом обещала сверку по ``id``.
    Расхождение не теоретическое: серверы шлют ``notifications/message``
    (логи) НЕЗАПРОШЕННО, и такая строка становилась «ответом», а настоящий
    ответ оставался непрочитанным.

    Сервер здесь шлёт уведомление ПЕРЕД ответом на ``initialize``.
    """
    request = aget_transport(_stdio_cfg(CHATTY="1"))
    _headers, body = await request("", {}, dict(_INIT_PAYLOAD), 30.0)

    assert body.get("method") != "notifications/message", (
        "уведомление сервера принято за ответ на initialize"
    )
    assert body.get("result"), f"настоящий ответ не прочитан: {body!r}"
    assert body.get("id") == _INIT_PAYLOAD["id"], "ответ с чужим id"


def test_response_to_a_foreign_id_is_skipped():
    """Ответ с чужим ``id`` пропускается, а не выдаётся за наш."""
    from gc_core.tools.connect_mcp.transport import _aread_response

    class _Reader:
        def __init__(self, lines):
            self._lines = list(lines)

        async def readline(self):
            return self._lines.pop(0) if self._lines else b""

    class _Proc:
        def __init__(self, lines):
            self.stdout = _Reader(lines)

    proc = _Proc([
        json.dumps({"jsonrpc": "2.0", "method": "notifications/message"}).encode() + b"\n",
        json.dumps({"jsonrpc": "2.0", "id": 77, "result": {"чужое": True}}).encode() + b"\n",
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"наше": True}}).encode() + b"\n",
    ])
    got = asyncio.run(_aread_response(proc, 5.0, 1))
    assert got.get("result") == {"наше": True}, f"взят не тот ответ: {got!r}"


# --- M-4: закрытие из чужой задачи не трогает чужую сессию -----------------

class _FakeStack:
    """Контекст-менеджер сессии: помнит, закрывали ли его."""

    def __init__(self) -> None:
        self.closed = False

    async def __aexit__(self, *exc) -> None:
        self.closed = True


async def test_closing_does_not_touch_a_neighbours_session():
    """``aclose_all`` закрывает СВОИ сессии и не трогает чужие.

    Тот самый случай: параллельный агент в этот момент внутри вызова тула, и
    закрытие из нашей задачи отменило бы его anyio-scope. Ошибка при этом
    вылезает не там, где сделана.
    """
    from gc_core.tools.connect_mcp.stream_client import _PersistentSessionManager

    registry = _PersistentSessionManager()
    mine, theirs = _FakeStack(), _FakeStack()

    with use_owner(new_owner_token()):
        registry.set_session("сервер", object(), (mine,))
        my_owner_had_it = registry.get_session("сервер") is not None

    with use_owner(new_owner_token()):
        registry.set_session("сервер", object(), (theirs,))
        # Чужая сессия под тем же ключом сервера, но другим владельцем.
        assert registry.get_session("сервер") is not None
        await registry.aclose_all()

    assert my_owner_had_it, "сессия не завелась у первого владельца"
    assert theirs.closed, "своя сессия не закрыта"
    assert not mine.closed, (
        "закрыта ЧУЖАЯ сессия: параллельный агент мог быть внутри вызова тула, "
        "и его anyio-scope отменился бы из нашей задачи"
    )


async def test_sessions_of_different_owners_do_not_collide():
    """Один и тот же сервер у двух владельцев — две разные сессии.

    Без токена владельца оба агента получили бы одну сессию, и закрытие одним
    рвало бы работу второго.
    """
    from gc_core.tools.connect_mcp.stream_client import _PersistentSessionManager

    registry = _PersistentSessionManager()
    first, second = object(), object()

    with use_owner(new_owner_token()):
        registry.set_session("общий", first, (_FakeStack(),))
        got_first = registry.get_session("общий")

    with use_owner(new_owner_token()):
        registry.set_session("общий", second, (_FakeStack(),))
        got_second = registry.get_session("общий")

    assert got_first is first and got_second is second
    assert got_first is not got_second, "владельцы поделили одну сессию"


async def test_close_all_refuses_from_a_running_loop():
    """Синхронный ``close_all`` из-под живого loop — громкий отказ.

    Молчаливый вариант дал бы вложенный ``asyncio.run``, а он падает не там,
    где ошибка.
    """
    from gc_core.tools.connect_mcp.stream_client import _PersistentSessionManager

    registry = _PersistentSessionManager()
    with pytest.raises(RuntimeError, match="aclose_all"):
        registry.close_all()


# --- M-10: транспорты -------------------------------------------------------

@pytest.mark.parametrize("transport", ["http", "sse", "stdio"])
def test_every_declared_transport_resolves(transport):
    """Все три транспорта отдают функцию запроса.

    Их именно три: ``mtls`` и ``headers`` — не транспорты, а опции поверх
    ``http``/``sse`` (проверяются в ``test_mcp_mtls_config`` и
    ``test_mcp_custom_headers``). Плановая формулировка «шесть транспортов»
    была неверной — здесь исправлено по коду.
    """
    cfg = {"transport": transport}
    if transport == "stdio":
        cfg.update({"command": "echo", "args": ["{}"]})
    assert callable(aget_transport(cfg))


def test_unknown_transport_is_rejected_by_name():
    """Неизвестный транспорт отвергается с перечислением годных."""
    with pytest.raises(ValueError) as err:
        aget_transport({"transport": "выдуманный"})
    text = str(err.value)
    assert "выдуманный" in text, "отказ не назвал, что именно передали"
    for name in ("http", "sse", "stdio"):
        assert name in text, f"отказ не перечислил {name}"


def test_missing_transport_is_rejected():
    """Транспорт не указан вовсе — тоже отказ, а не молчаливый дефолт."""
    with pytest.raises(ValueError):
        aget_transport({})


async def test_acall_mcp_tool_does_not_freeze_the_loop_on_discovery(monkeypatch):
    """Дискавери без готового реестра не должна морозить loop вызывающего.

    ``acall_mcp_tool(tools_registry=None)`` шёл в синхронный ``get_all_tools``,
    а тот — в ``run_coro_blocking``: свой ``asyncio.run`` внутри чужого loop
    невозможен, поэтому обход серверов исполнялся, держа loop. Асинхронная
    форма у кэша была всё это время.

    Проверяется отзывчивость, а не форма вызова: пока дискавери идёт, соседний
    хартбит обязан тикать.
    """
    import asyncio

    from gc_core.tools.connect_mcp import api as mcp_api
    from gc_core.tools.connect_mcp import registry as mcp_registry

    async def _slow_discovery(self, *, on_progress=None):
        await asyncio.sleep(0.3)
        return {}

    monkeypatch.setattr(mcp_registry.ToolsCache, "aget_tools", _slow_discovery)

    def _sync_trap(*_a, **_kw):
        raise AssertionError("дискавери ушла в синхронную форму и заморозила loop")

    monkeypatch.setattr(mcp_registry, "get_all_tools", _sync_trap)
    monkeypatch.setattr(mcp_api, "get_all_tools", _sync_trap, raising=False)

    ticks = 0

    async def _heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(_heartbeat())
    try:
        with pytest.raises(ValueError):        # реестр пуст → тул неизвестен
            await mcp_api.acall_mcp_tool(
                "mcp_srv_tool", {}, {"srv": {"transport": "stdio", "command": "x"}},
                tools_registry=None,
            )
    finally:
        beat.cancel()

    assert ticks >= 10, f"loop стоял во время дискавери: тиков {ticks}"
