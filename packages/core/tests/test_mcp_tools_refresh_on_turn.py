"""Набор MCP-тулов контекста обновляется на входе в ход (спека §5.5–§5.6).

На входе в ход потребитель сервиса, ничего не дожидаясь, назначает
подключение неподключённым серверам, в фоне перечитывает список тулов у
давно проверенных сетевых серверов и сообщает, изменился ли срез; если
изменился — исполнитель пересобирает набор тулов контекста до хода. Без
потребителя ход прежний. Исполнитель, цикл и тулы настоящие; скриптуется
только ответ модели. Сроки — по подставным часам сервиса.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from gc_core import agent_executor
from gc_core.agent_executor import AgentExecutor
from gc_core.mcp_session_owner import current_owner
from gc_core.run_context import acreate_run_context
from gc_core.thread_store import _thread_paths, resolve_threads_dir
from gc_core.tools.connect_mcp import (
    consumer_connections as connections_module,
)
from gc_core.tools.connect_mcp import stream_client
from gc_core.tools.connect_mcp.consumer_status import (
    STATE_CONNECTED,
    STATE_FAILED,
)
from gc_core.tools.connect_mcp.registry import (
    ToolsCache,
    mcp_tool_full_name,
)
from gc_core.tools.connect_mcp.service import McpService
from gc_core.user_stop import UserStop, bind_user_stop, current_user_stop
from gc_core.utils.logging_module import release_run_logger
from gc_testkit.mcp import (
    FakeClock,
    events,
    free_port,
    launches,
    set_behavior,
    stdio_cfg,
    stream_server,
    wait_for,
)
from gc_testkit.mcp.fixtures import mcp_home, mcp_owner, mcp_reaper
from gc_testkit.settings import make_settings
from parity_harness import (
    ScriptedBackend,
    assistant_tool_call,
    install,
    parity_config,
)

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="процессы и сигналы POSIX"
)

#: Потолок одного ожидания в тесте: зависание не повесит прогон.
_CEILING_SEC = 30.0
#: Срок свежести списка по умолчанию (``mcp.meta.tools-cache-ttl``).
_TTL_SEC = 3600
_ECHO = mcp_tool_full_name("srv", "echo")
_NAP = mcp_tool_full_name("srv", "nap")
_FINISH_ARGS = {
    "solution_type": "full_solution",
    "task": "Ответить",
    "task_report": "Готово",
    "handoff": "{}",
}
_CONFIG = {
    "agent.max_steps": 15,
    "agent.strict_turn_finish_validation": False,
    "text_answer_mode": "deny",
    "intent_scope.tools": {},
    "mcp.meta.discovery_timeout_sec": 30,
}


def _cfg(tmp_path, monkeypatch, servers: dict):
    return parity_config(
        tmp_path, monkeypatch, **{**_CONFIG, "mcp.servers": servers}
    )


def _settings(servers: dict, *, discovery: int = 30):
    return make_settings(
        {
            "mcp": {
                "servers": servers,
                "meta": {"discovery_timeout_sec": discovery},
            }
        }
    )


def _finish(n: int) -> dict:
    return assistant_tool_call("turn_finish", _FINISH_ARGS, f"fin-{n}")


def _closed_port_cfg() -> dict:
    return {
        "transport": "stream_http",
        "url": f"http://127.0.0.1:{free_port()}",
    }


def _consumer(reaper, clock: FakeClock, *, on_start: bool = True):
    """Потребитель сервиса с подставными часами; закроет уборка."""
    service = McpService(
        connect_on_start=on_start, clock=clock.now, sleep=clock.sleep
    )
    return reaper.track(service.open_consumer("окно"))


def _conn(consumer):
    """Соединения потребителя — владелец его состояния."""
    return consumer._connections


@contextlib.asynccontextmanager
async def _context(settings, **kwargs):
    ctx = await asyncio.wait_for(
        acreate_run_context(settings, **kwargs), _CEILING_SEC
    )
    try:
        yield ctx
    finally:
        release_run_logger(ctx.logger)


async def _turn(executor: AgentExecutor, thread_id: str, **kwargs):
    return await asyncio.wait_for(
        executor.arun(
            ["Ответь"],
            thread_id=thread_id,
            system="cli",
            start_new_thread=True,
            message_ids=[f"in-{thread_id}"],
            **kwargs,
        ),
        _CEILING_SEC,
    )


def _tool_names(payload: dict) -> set[str]:
    """Имена тулов в запросе к модели."""
    return {spec["function"]["name"] for spec in payload.get("tools", [])}


def _meta_mcp(settings, thread_id: str) -> dict:
    """Снимок MCP в мете треда (экран ``/mcp``)."""
    _data, meta_path = _thread_paths(
        resolve_threads_dir(settings), "cli", thread_id
    )
    meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    return meta["runtime"]["mcp_tools"]


def _requests(control, method: str) -> list[int]:
    """Номера сессий потокового сервера по запросам ``method``."""
    prefix = f"req {method} "
    return [
        int(line.split()[3])
        for line in events(control)
        if line.startswith(prefix)
    ]


def _count_lists(monkeypatch) -> list[str]:
    """Каждое обращение потребителя за списком; транспорт настоящий."""
    calls: list[str] = []
    real = connections_module.alist_raw_tools

    async def counting(server_key, server_cfg, *, timeout):
        calls.append(server_key)
        return await real(server_key, server_cfg, timeout=timeout)

    monkeypatch.setattr(connections_module, "alist_raw_tools", counting)
    return calls


async def test_recovered_server_appears_on_next_turn(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    control = tmp_path / "srv.json"
    set_behavior(control, fail_start=True)
    settings = _cfg(tmp_path, monkeypatch, {"srv": stdio_cfg(control)})
    monkeypatch.chdir(tmp_path)
    backend = install(
        monkeypatch, ScriptedBackend([_finish(1), _finish(2), _finish(3)])
    )
    consumer = _consumer(mcp_reaper, clock)

    async with _context(settings, mcp_consumer=consumer) as ctx:
        executor = AgentExecutor(ctx)
        await _turn(executor, "t1")
        set_behavior(control)
        clock.advance(30)
        await _turn(executor, "t2")
        entry = consumer.servers()["srv"]
        await wait_for(
            lambda: consumer.status_of(entry).state == STATE_CONNECTED,
            _CEILING_SEC,
        )
        await _turn(executor, "t3")
        status = ctx.mcp_status["srv"]["status"]
        spec_names = {spec["name"] for spec in ctx.tools_spec}
        fields = (ctx.tools_data, ctx.registered_tool_names, spec_names)

    backend.assert_clean()
    names = [_tool_names(call) for call in backend.calls]
    assert _ECHO not in names[0]
    assert _ECHO not in names[1]
    assert _ECHO in names[2]
    assert status == STATE_CONNECTED
    assert _meta_mcp(settings, "t3")["srv"]["status"] == STATE_CONNECTED
    # Пересборка заменила поля контекста согласованно.
    assert all(_ECHO in field for field in fields)


async def test_changed_tool_list_is_visible_on_next_turn(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    settings = _cfg(tmp_path, monkeypatch, {"srv": stdio_cfg(control)})
    monkeypatch.chdir(tmp_path)
    backend = install(monkeypatch, ScriptedBackend([_finish(1), _finish(2)]))
    consumer = _consumer(mcp_reaper, FakeClock())

    async with _context(settings, mcp_consumer=consumer) as ctx:
        executor = AgentExecutor(ctx)
        await _turn(executor, "t1")
        set_behavior(control, tools=["echo"])
        entry = consumer.servers()["srv"]
        assert await asyncio.wait_for(
            _conn(consumer).start_connect(entry), _CEILING_SEC
        )
        await _turn(executor, "t2")

    backend.assert_clean()
    first, second = (_tool_names(call) for call in backend.calls)
    assert {_ECHO, _NAP} <= first
    assert _ECHO in second
    assert _NAP not in second
    # Тот же процесс: список перечитан по живой сессии.
    assert len(launches(control)) == 1


class _ChangeOnFirstCall(ScriptedBackend):
    """Сценарий модели, который на первом запросе что-то меняет."""

    def __init__(self, turns: list[dict], on_first) -> None:
        super().__init__(turns)
        self._on_first = on_first

    def next_payload(self, request_payload: dict | None) -> dict:
        if not self.calls:
            self._on_first()
        return super().next_payload(request_payload)


async def test_reply_from_queue_keeps_the_set(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    settings = _cfg(tmp_path, monkeypatch, {"srv": stdio_cfg(control)})
    monkeypatch.chdir(tmp_path)
    consumer = _consumer(mcp_reaper, FakeClock())

    async with _context(settings, mcp_consumer=consumer) as ctx:
        executor = AgentExecutor(ctx)
        entry = consumer.servers()["srv"]

        def change_and_queue():
            # Во время хода список сервера сменился, реплика — в очереди.
            echo_only = [t for t in entry.tools if t["name"] == "echo"]
            consumer._service.record_success(entry, echo_only)
            executor.add_message({"role": "user", "content": "ещё"})

        backend = install(
            monkeypatch,
            _ChangeOnFirstCall(
                [_finish(1), _finish(2), _finish(3)], change_and_queue
            ),
        )
        await _turn(executor, "t1")
        await _turn(executor, "t2")

    backend.assert_clean()
    names = [_tool_names(call) for call in backend.calls]
    # Подхват из очереди идёт на наборе этого прогона.
    assert _NAP in names[0]
    assert _NAP in names[1]
    # Следующий вход в ход видит изменение.
    assert _NAP not in names[2]


async def test_stale_network_entry_is_relisted_over_session(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    monkeypatch.chdir(tmp_path)
    backend = install(monkeypatch, ScriptedBackend([_finish(1), _finish(2)]))
    async with stream_server(tmp_path, "stream_http") as (network, control):
        settings = _cfg(
            tmp_path,
            monkeypatch,
            {"srv": {**network, "persistent_session": True}},
        )
        consumer = _consumer(mcp_reaper, clock)
        async with _context(settings, mcp_consumer=consumer) as ctx:
            executor = AgentExecutor(ctx)
            tools_before = ctx.tools_data
            clock.advance(_TTL_SEC + 1)
            await _turn(executor, "t1")
            await wait_for(
                lambda: len(_requests(control, "tools/list")) == 2,
                _CEILING_SEC,
            )
            await wait_for(lambda: not _conn(consumer)._refreshes)
            entry = consumer.servers()["srv"]
            checked_at = entry.checked_at
            state = consumer.status_of(entry).state
            await _turn(executor, "t2")
            tools_after = ctx.tools_data
        await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)

    backend.assert_clean()
    [session] = _requests(control, "initialize")
    assert _requests(control, "tools/list") == [session, session]
    assert checked_at == _TTL_SEC + 1
    assert state == STATE_CONNECTED
    # Время проверки на пересборку не влияет: список тот же.
    assert tools_after is tools_before


async def test_slow_relist_keeps_live_session(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    real_list = stream_client._alist_tools_on_session

    async def slow_list(session):
        await asyncio.sleep(_CEILING_SEC * 2)
        return await real_list(session)

    async with stream_server(tmp_path, "stream_http") as (network, control):
        settings = _settings(
            {"srv": {**network, "persistent_session": True}}, discovery=2
        )
        consumer = _consumer(mcp_reaper, clock)
        await asyncio.wait_for(
            consumer.aensure(consumer.next_build(), settings), _CEILING_SEC
        )
        entry = consumer.servers()["srv"]
        monkeypatch.setattr(
            stream_client, "_alist_tools_on_session", slow_list
        )
        clock.advance(_TTL_SEC + 1)

        assert await consumer.astart_turn() is False
        await wait_for(lambda: not _conn(consumer)._refreshes, _CEILING_SEC)
        slow_state = consumer.status_of(entry).state
        kept = _conn(consumer).has_session(entry)
        # Неудачное перечитывание повторится только через срок свежести.
        await consumer.astart_turn()
        no_retry = not _conn(consumer)._refreshes
        monkeypatch.setattr(
            stream_client, "_alist_tools_on_session", real_list
        )
        clock.advance(_TTL_SEC + 1)
        await consumer.astart_turn()
        await wait_for(
            lambda: len(_requests(control, "tools/list")) == 2, _CEILING_SEC
        )
        await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)

    assert slow_state == STATE_CONNECTED
    assert kept
    assert no_retry
    [session] = _requests(control, "initialize")
    assert _requests(control, "tools/list") == [session, session]


async def test_relist_failure_is_a_link_failure(
    mcp_home, monkeypatch, mcp_reaper, mcp_owner, caplog
):
    clock = FakeClock()
    calls: list[str] = []

    async def fake_list(server_key, server_cfg, *, timeout):
        calls.append(server_key)
        if len(calls) > 1:
            raise ConnectionError("сервер пропал")
        return [{"name": "echo"}]

    monkeypatch.setattr(connections_module, "alist_raw_tools", fake_list)
    settings = _settings({"srv": _closed_port_cfg()})
    consumer = _consumer(mcp_reaper, clock, on_start=False)
    await asyncio.wait_for(
        consumer.aensure(consumer.next_build(), settings), _CEILING_SEC
    )
    entry = consumer.servers()["srv"]
    assert consumer.status_of(entry).state == STATE_CONNECTED
    clock.advance(_TTL_SEC + 1)

    with caplog.at_level(logging.WARNING, logger=connections_module.__name__):
        assert await consumer.astart_turn() is False
        await wait_for(
            lambda: consumer.status_of(entry).state == STATE_FAILED,
            _CEILING_SEC,
        )
        await wait_for(lambda: not _conn(consumer)._refreshes)

    assert len(calls) == 2
    assert "перечитывание списка тулов" in caplog.text
    # Отказ связи виден на следующем входе в ход; ленивый потребитель
    # сам не переподключается.
    assert await consumer.astart_turn() is True
    assert len(calls) == 2


async def _gated_relist(monkeypatch, reaper, clock, release):
    """Ленивый потребитель, подключённый сервер; перечитывание списка ждёт
    ``release`` и затем бросает. Отдаёт потребителя и запись сервера."""
    calls: list[str] = []

    async def fake_list(server_key, server_cfg, *, timeout):
        calls.append(server_key)
        if len(calls) > 1:
            await release.wait()
            raise ConnectionError("сервер пропал")
        return [{"name": "echo"}]

    monkeypatch.setattr(connections_module, "alist_raw_tools", fake_list)
    consumer = _consumer(reaper, clock, on_start=False)
    settings = _settings({"srv": _closed_port_cfg()})
    await asyncio.wait_for(
        consumer.aensure(consumer.next_build(), settings), _CEILING_SEC
    )
    clock.advance(_TTL_SEC + 1)
    await consumer.astart_turn()
    await wait_for(lambda: len(calls) == 2)
    return consumer, consumer.servers()["srv"]


async def test_relist_over_dropped_connection_is_not_a_failure(
    mcp_home, monkeypatch, mcp_reaper, mcp_owner
):
    release = asyncio.Event()
    consumer, entry = await _gated_relist(
        monkeypatch, mcp_reaper, FakeClock(), release
    )

    # Соединение сняли за время списка (сбой вызова, сверка).
    await asyncio.wait_for(_conn(consumer).adrop(entry), _CEILING_SEC)
    release.set()
    await wait_for(lambda: not _conn(consumer)._refreshes)

    assert _conn(consumer).own_failure(entry) is None
    assert entry.last_failure is None


async def test_server_leaving_set_cancels_relist(
    mcp_home, monkeypatch, mcp_reaper, mcp_owner
):
    consumer, entry = await _gated_relist(
        monkeypatch, mcp_reaper, FakeClock(), asyncio.Event()
    )
    [running] = _conn(consumer)._refreshes.values()

    await asyncio.wait_for(
        consumer.aensure(consumer.next_build(), _settings({})), _CEILING_SEC
    )
    await asyncio.wait_for(asyncio.wait({running}), _CEILING_SEC)

    assert running.cancelled()
    assert entry.last_failure is None


async def test_close_cancels_relist(
    mcp_home, monkeypatch, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    seen: list = []

    async def fake_list(server_key, server_cfg, *, timeout):
        seen.append((current_user_stop(), current_owner()))
        if len(seen) > 1:
            await asyncio.Event().wait()
        return [{"name": "echo"}]

    monkeypatch.setattr(connections_module, "alist_raw_tools", fake_list)
    settings = _settings({"srv": _closed_port_cfg()})
    consumer = _consumer(mcp_reaper, clock, on_start=False)
    await asyncio.wait_for(
        consumer.aensure(consumer.next_build(), settings), _CEILING_SEC
    )
    clock.advance(_TTL_SEC + 1)

    with bind_user_stop(UserStop()):
        await consumer.astart_turn()
    await wait_for(lambda: len(seen) == 2)
    running = list(_conn(consumer)._refreshes.values())
    await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)

    assert len(running) == 1
    assert running[0].cancelled()
    # Фоновое перечитывание не видит стопа хода, владелец — потребитель.
    assert seen[1] == (None, consumer.owner)


async def test_resume_after_question_rereads_the_set(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    settings = _cfg(tmp_path, monkeypatch, {"srv": stdio_cfg(control)})
    monkeypatch.chdir(tmp_path)
    ask = assistant_tool_call(
        "turn_ask_human", {"question": "A или B?"}, "ask-1"
    )
    backend = install(monkeypatch, ScriptedBackend([ask, _finish(1)]))
    consumer = _consumer(mcp_reaper, FakeClock())

    async with _context(settings, mcp_consumer=consumer) as ctx:
        ctx.ask_human_suspends = True
        executor = AgentExecutor(ctx)
        await _turn(executor, "t1")
        assert executor.last_terminal_outcome == "input_required"
        set_behavior(control, tools=["echo"])
        entry = consumer.servers()["srv"]
        assert await asyncio.wait_for(
            _conn(consumer).start_connect(entry), _CEILING_SEC
        )
        await asyncio.wait_for(
            executor.arun([], answer="B", thread_id="t1", system="cli"),
            _CEILING_SEC,
        )

    backend.assert_clean()
    asked, answered = (_tool_names(call) for call in backend.calls)
    assert _NAP in asked
    assert _ECHO in answered
    assert _NAP not in answered


async def test_failed_server_gets_attempt_on_turn_start(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    control = tmp_path / "srv.json"
    set_behavior(control, fail_start=True)
    settings = _cfg(tmp_path, monkeypatch, {"srv": stdio_cfg(control)})
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, ScriptedBackend([_finish(1), _finish(2), _finish(3)]))
    consumer = _consumer(mcp_reaper, clock)

    async with _context(settings, mcp_consumer=consumer) as ctx:
        executor = AgentExecutor(ctx)
        await _turn(executor, "t1")
        await _turn(executor, "t2")
        before = len(launches(control))
        clock.advance(30)
        await _turn(executor, "t3")
        await wait_for(lambda: len(launches(control)) == 2, _CEILING_SEC)
        await wait_for(lambda: not _conn(consumer)._connects, _CEILING_SEC)

    assert before == 1


async def test_turn_start_during_retry_series_adds_no_attempt(
    mcp_home, monkeypatch, mcp_reaper, mcp_owner
):
    clock = FakeClock()
    pauses: list[float] = []

    async def parked_sleep(sec: float) -> None:
        pauses.append(sec)
        await asyncio.Event().wait()

    attempts = _count_lists(monkeypatch)
    settings = _settings({"srv": _closed_port_cfg()})
    service = McpService(
        connect_on_start=True, clock=clock.now, sleep=parked_sleep
    )
    consumer = mcp_reaper.track(service.open_consumer("окно"))
    await asyncio.wait_for(
        consumer.aensure(consumer.next_build(), settings), _CEILING_SEC
    )
    await wait_for(lambda: pauses == [1], _CEILING_SEC)
    clock.advance(30)

    assert await consumer.astart_turn() is False
    assert len(attempts) == 1
    assert not _conn(consumer)._connects


@pytest.mark.parametrize("broken", ["start_turn", "rebuild"])
async def test_refresh_failure_does_not_break_turn(
    tmp_path, monkeypatch, broken
):
    settings = _cfg(tmp_path, monkeypatch, {})
    monkeypatch.chdir(tmp_path)
    backend = install(monkeypatch, ScriptedBackend([_finish(1)]))

    async def start_turn() -> bool:
        if broken == "start_turn":
            raise RuntimeError("начало хода упало")
        return True

    async def rebuild(ctx) -> None:
        raise RuntimeError("пересборка упала")

    monkeypatch.setattr(
        agent_executor, "arefresh_context_tools", rebuild, raising=False
    )
    async with _context(settings) as ctx:
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append
        ctx.logger.addHandler(handler)
        ctx.tools_cache = SimpleNamespace(astart_turn=start_turn)
        try:
            result = await _turn(AgentExecutor(ctx), "t1")
        finally:
            ctx.logger.removeHandler(handler)

    backend.assert_clean()
    assert result
    [warning] = [r for r in records if r.msg == "mcp_tools_refresh_failed"]
    assert warning.exc_info is not None


@pytest.mark.parametrize("cache", [False, True])
async def test_context_without_consumer_untouched(
    tmp_path, monkeypatch, cache
):
    settings = _cfg(tmp_path, monkeypatch, {})
    monkeypatch.chdir(tmp_path)
    backend = install(monkeypatch, ScriptedBackend([_finish(1)]))
    rebuilt: list = []

    async def rebuild(ctx) -> None:
        rebuilt.append(ctx)

    monkeypatch.setattr(
        agent_executor, "arefresh_context_tools", rebuild, raising=False
    )
    kwargs = {"tools_cache": ToolsCache()} if cache else {}
    async with _context(settings, **kwargs) as ctx:
        before = (ctx.tools_data, ctx.tools_spec, ctx.registered_tool_names)
        await _turn(AgentExecutor(ctx), "t1")
        after = (ctx.tools_data, ctx.tools_spec, ctx.registered_tool_names)

    backend.assert_clean()
    assert all(a is b for a, b in zip(before, after))
    assert rebuilt == []
