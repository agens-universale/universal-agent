"""Сессии серверов с trace у потребителя сервиса MCP живут один ход (§5.9).

У сервера с ``trace_header_enabled`` trace хода зашит в сессию (заголовок
или окружение процесса), поэтому при заданном trace конец хода закрывает
его сессию, и следующий ход открывает новую. Без trace такой сервер живёт
между ходами, как обычный. Trace берётся из настроек последней сверенной
сборки; в отпечаток и каталог он не входит.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest
from gc_core.agent_executor import AgentExecutor
from gc_core.run_context import acreate_run_context
from gc_core.tools.connect_mcp import consumer as consumer_module
from gc_core.tools.connect_mcp import (
    consumer_connections as connections_module,
)
from gc_core.tools.connect_mcp import consumer_executor as executor_module
from gc_core.tools.connect_mcp.consumer_call import SESSION_LOST_TEXT
from gc_core.tools.connect_mcp.consumer_status import (
    STATE_CACHED,
    STATE_CONNECTED,
)
from gc_core.tools.connect_mcp.registry import mcp_tool_full_name
from gc_core.tools.connect_mcp.service import McpService
from gc_core.tools.connect_mcp.stream_client import McpToolCallError
from gc_core.tools.connect_mcp.tool_wrapper import MCPToolWrapper
from gc_core.utils.logging_module import release_run_logger
from gc_testkit.mcp import (
    FakeClock,
    alive,
    children,
    events,
    launches,
    seed_catalog,
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
    run_thread_dump,
)

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="процессы и сигналы POSIX"
)

#: Потолок одного ожидания в тесте: зависание не повесит прогон.
_CEILING_SEC = 30.0
#: Мягкий срок закрытия процесса сервера (``persistent_stdio``) и запас.
_GRACEFUL_SEC = 5.0
_SLACK_SEC = 3.0
_TRACE = "trace-a13-1"
_ECHO = mcp_tool_full_name("srv", "echo")
_NAP = mcp_tool_full_name("srv", "nap")
_FINISH_ARGS = {
    "solution_type": "full_solution",
    "task": "Вызвать тул",
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


def _traced(control) -> dict:
    """Процессный сервер, которому разрешён trace (окружение процесса)."""
    return {**stdio_cfg(control), "trace_header_enabled": True}


def _settings(servers: dict, trace: str = ""):
    runtime = {"trace_id": trace} if trace else {}
    return make_settings(
        {
            "mcp": {
                "servers": servers,
                "meta": {"discovery_timeout_sec": 30},
            },
            **runtime,
        }
    )


def _conn(consumer):
    """Соединения потребителя — владелец его состояния."""
    return consumer._connections


def _consumer(reaper, clock: FakeClock, *, on_start: bool = True):
    """Потребитель сервиса с подставными часами: правило 30 с без
    сдвига часов не пустило бы повторное подключение после отказа."""
    service = McpService(
        connect_on_start=on_start, clock=clock.now, sleep=clock.sleep
    )
    return reaper.track(service.open_consumer("окно"))


async def _ensure(consumer, settings, build: int | None = None) -> None:
    if build is None:
        build = consumer.next_build()
    await asyncio.wait_for(consumer.aensure(build, settings), _CEILING_SEC)


async def _end_turn(consumer) -> None:
    await asyncio.wait_for(consumer.aend_turn(), _CEILING_SEC)


def _wrapper(view, name: str) -> MCPToolWrapper:
    """Обёртка тула из среза — как её строит сборка контекста."""
    return MCPToolWrapper(
        name,
        view.servers_cfg,
        view.registry,
        20.0,
        default_use_retry=False,
        caller=view.caller,
    )


async def _echo(consumer, settings, name: str = _ECHO) -> dict:
    """Вызов ``echo`` через срез потребителя; ответ тестового сервера."""
    return await _call(consumer, settings, name, text="hi")


async def _call(consumer, settings, name: str, **arguments) -> dict:
    """Вызов тула через срез потребителя; ответ тестового сервера."""
    view = await asyncio.wait_for(consumer.aview(settings), _CEILING_SEC)
    envelope = await asyncio.wait_for(
        _wrapper(view, name).acall(**arguments), _CEILING_SEC
    )
    result = json.loads(envelope)["result"]
    if isinstance(result, str):
        return json.loads(result)
    return json.loads(result["content"][0]["text"])


# --- ходы настоящего исполнителя ------------------------------------------


def _echo_turn(call_id: str) -> list[dict]:
    """Ходы модели: вызвать ``echo``, затем закончить ход."""
    return [
        assistant_tool_call(_ECHO, {"text": call_id}, f"echo-{call_id}"),
        assistant_tool_call("turn_finish", _FINISH_ARGS, f"fin-{call_id}"),
    ]


def _echo_replies(settings, thread_id: str) -> list[dict]:
    """Ответы тестового сервера на ``echo`` в треде."""
    replies: list[dict] = []
    called = None
    for row in run_thread_dump(settings, "cli", thread_id):
        payload = row["payload"]
        if payload.get("type") == "function_call":
            called = payload.get("name")
        elif payload.get("type") == "function_call_output":
            if called == _ECHO:
                (content,) = json.loads(payload["output"])["result"]["content"]
                replies.append(json.loads(content["text"]))
            called = None
    return replies


async def _two_turns(tmp_path, monkeypatch, reaper, trace: str) -> list:
    """Два хода исполнителя с вызовом ``echo`` на одном контексте;
    ответы сервера по ходам."""
    control = tmp_path / "srv.json"
    settings = parity_config(
        tmp_path,
        monkeypatch,
        **{**_CONFIG, "mcp.servers": {"srv": _traced(control)}},
    )
    if trace:
        settings = settings.with_runtime(trace_id=trace)
    monkeypatch.chdir(tmp_path)
    backend = install(
        monkeypatch, ScriptedBackend(_echo_turn("1") + _echo_turn("2"))
    )
    consumer = _consumer(reaper, FakeClock())
    ctx = await asyncio.wait_for(
        acreate_run_context(settings, mcp_consumer=consumer), _CEILING_SEC
    )
    try:
        executor = AgentExecutor(ctx)
        for thread_id in ("t1", "t2"):
            await asyncio.wait_for(
                executor.arun(
                    ["Вызови тул"],
                    thread_id=thread_id,
                    system="cli",
                    start_new_thread=True,
                    message_ids=[f"in-{thread_id}"],
                ),
                _CEILING_SEC,
            )
    finally:
        release_run_logger(ctx.logger)
    backend.assert_clean()
    return [
        reply
        for thread_id in ("t1", "t2")
        for reply in _echo_replies(settings, thread_id)
    ]


async def test_trace_server_gets_new_session_each_turn(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    first, second = await _two_turns(tmp_path, monkeypatch, mcp_reaper, _TRACE)

    assert first["trace"] == second["trace"] == _TRACE
    assert first["pid"] != second["pid"]
    # Сессия первого хода закрыта концом хода, а не брошена.
    assert not alive(first["pid"])
    assert launches(tmp_path / "srv.json") == [first["pid"], second["pid"]]


async def test_trace_flag_without_trace_keeps_session(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    first, second = await _two_turns(tmp_path, monkeypatch, mcp_reaper, "")

    assert first["trace"] == second["trace"] == ""
    assert first["pid"] == second["pid"]
    assert launches(tmp_path / "srv.json") == [first["pid"]]


# --- потребитель напрямую -------------------------------------------------


async def test_connect_carries_build_trace(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    settings = _settings({"srv": _traced(control)}, _TRACE)
    consumer = _consumer(mcp_reaper, FakeClock())

    # Сервис «на старте»: сверка сама подключает сервер до первого хода.
    await _ensure(consumer, settings)
    [connected_pid] = launches(control)
    out = await _echo(consumer, settings)

    assert out["trace"] == _TRACE
    assert out["pid"] == connected_pid
    assert launches(control) == [connected_pid]


async def test_trace_not_in_catalog(mcp_home, tmp_path, mcp_reaper, mcp_owner):
    control = tmp_path / "srv.json"
    settings = _settings({"srv": _traced(control)}, _TRACE)
    consumer = _consumer(mcp_reaper, FakeClock())

    await _ensure(consumer, settings)
    files = [path for path in mcp_home.rglob("*") if path.is_file()]

    assert consumer.status_of(consumer.servers()["srv"]).state == (
        STATE_CONNECTED
    )
    assert files
    assert all(_TRACE not in p.read_text(errors="replace") for p in files)


async def test_server_without_trace_flag_keeps_session(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    traced, plain = tmp_path / "srv.json", tmp_path / "plain.json"
    settings = _settings(
        {"srv": _traced(traced), "plain": stdio_cfg(plain)}, _TRACE
    )
    consumer = _consumer(mcp_reaper, FakeClock())
    await _ensure(consumer, settings)
    [traced_pid], [plain_pid] = launches(traced), launches(plain)

    await _end_turn(consumer)

    servers = consumer.servers()
    assert not alive(traced_pid)
    assert alive(plain_pid)
    assert consumer.status_of(servers["plain"]).state == STATE_CONNECTED
    assert consumer.status_of(servers["srv"]).state == STATE_CACHED
    out = await _echo(consumer, settings, mcp_tool_full_name("plain", "echo"))
    assert out["pid"] == plain_pid


async def test_turn_end_takes_trace_of_latest_build(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    servers = {"srv": _traced(control)}
    consumer = _consumer(mcp_reaper, FakeClock())
    await _ensure(consumer, _settings(servers))
    [pid] = launches(control)

    # Тот же сервер (trace в отпечаток не входит), новая сборка — с trace.
    traced = _settings(servers, _TRACE)
    await _ensure(consumer, traced)
    await _end_turn(consumer)

    assert not alive(pid)
    # Новая сессия несёт trace текущей сборки.
    out = await _echo(consumer, traced)
    assert out["trace"] == _TRACE
    assert launches(control) == [pid, out["pid"]]


async def test_stale_build_does_not_change_trace(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    servers = {"srv": _traced(control)}
    consumer = _consumer(mcp_reaper, FakeClock())
    stale, latest = consumer.next_build(), consumer.next_build()
    await _ensure(consumer, _settings(servers), latest)
    [pid] = launches(control)

    await _ensure(consumer, _settings(servers, _TRACE), stale)
    await _end_turn(consumer)

    assert alive(pid)
    assert consumer.status_of(consumer.servers()["srv"]).state == (
        STATE_CONNECTED
    )


@pytest.mark.parametrize("on_start", [True, False])
async def test_next_turn_reconnects_by_policy_without_30s_rule(
    mcp_home, tmp_path, mcp_reaper, mcp_owner, on_start
):
    control = tmp_path / "srv.json"
    settings = _settings({"srv": _traced(control)}, _TRACE)
    consumer = _consumer(mcp_reaper, FakeClock(), on_start=on_start)
    # Тулы неизвестны: сверка подключает сервер при любой политике.
    await _ensure(consumer, settings)
    [pid] = launches(control)
    entry = consumer.servers()["srv"]

    await _end_turn(consumer)
    status = consumer.status_of(entry)
    # Часы не сдвигались: отказ закрыл бы подключение на 30 с.
    assert status.state == STATE_CACHED
    assert status.tools == entry.tools
    assert _conn(consumer).own_failure(entry) is None
    assert entry.last_failure is None
    assert _conn(consumer).may_connect(entry)

    await asyncio.wait_for(consumer.astart_turn(), 1)
    _connected, attempt = _conn(consumer).attempt_of(entry)
    # «На старте» вход в ход назначает попытку, «при первом вызове» — нет.
    assert (attempt is not None) is on_start
    out = await _echo(consumer, settings)

    assert out["trace"] == _TRACE
    assert out["pid"] != pid
    assert launches(control) == [pid, out["pid"]]
    assert consumer.status_of(entry).state == STATE_CONNECTED


async def test_turn_end_during_connect_keeps_attempt(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    set_behavior(control, start_delay=1.0)
    settings = _settings({"srv": _traced(control)}, _TRACE)
    # Тулы известны из каталога: сверка «при первом вызове» не подключает.
    seed_catalog(settings, "srv", ["echo", "nap"])
    consumer = _consumer(mcp_reaper, FakeClock(), on_start=False)
    await _ensure(consumer, settings)
    entry = consumer.servers()["srv"]
    assert launches(control) == []
    waiting = _conn(consumer).start_connect(entry)
    await wait_for(lambda: bool(launches(control)), _CEILING_SEC)
    assert _conn(consumer).attempt_of(entry)[0] is False

    await _end_turn(consumer)

    # Подключение ещё не сессия: конец хода его не обрывает и отказом
    # не делает.
    assert await asyncio.wait_for(waiting, _CEILING_SEC)
    assert consumer.status_of(entry).state == STATE_CONNECTED
    assert _conn(consumer).own_failure(entry) is None
    assert entry.last_failure is None


async def test_call_in_flight_at_turn_end_is_not_a_failure(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    settings = _settings({"srv": _traced(control)}, _TRACE)
    consumer = _consumer(mcp_reaper, FakeClock())
    await _ensure(consumer, settings)
    entry = consumer.servers()["srv"]
    view = await asyncio.wait_for(consumer.aview(settings), _CEILING_SEC)
    call = asyncio.create_task(_wrapper(view, _NAP).acall(seconds=20))
    try:
        await wait_for(
            lambda: any(
                e.startswith("req tools/call") for e in events(control)
            ),
            _CEILING_SEC,
        )
        await _end_turn(consumer)
        done, _ = await asyncio.wait({call}, timeout=_CEILING_SEC)
    finally:
        call.cancel()
        await asyncio.wait({call}, timeout=_CEILING_SEC)

    assert done
    with pytest.raises(McpToolCallError) as caught:
        call.result()
    assert caught.value.error.message == SESSION_LOST_TEXT
    assert consumer.status_of(entry).state == STATE_CACHED
    assert _conn(consumer).own_failure(entry) is None
    assert entry.last_failure is None


async def test_turn_end_is_bounded_on_deaf_server(
    mcp_home, tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    set_behavior(
        control,
        ignore_eof=True,
        ignore_term=True,
        spawn_child=True,
        child_ignores_term=True,
    )
    settings = _settings({"srv": _traced(control)}, _TRACE)
    consumer = _consumer(mcp_reaper, FakeClock())
    await _ensure(consumer, settings)
    pids = launches(control) + children(control)
    assert len(pids) == 2

    started = time.monotonic()
    await _end_turn(consumer)
    elapsed = time.monotonic() - started

    assert elapsed < _GRACEFUL_SEC + _SLACK_SEC
    await wait_for(lambda: not any(alive(p) for p in pids), _CEILING_SEC)


@pytest.mark.parametrize("closing", ["consumer", "service"])
async def test_closing_consumer_turn_end_does_nothing(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner, closing
):
    control = tmp_path / "srv.json"
    settings = _settings({"srv": _traced(control)}, _TRACE)
    consumer = _consumer(mcp_reaper, FakeClock())
    await _ensure(consumer, settings)
    [pid] = launches(control)
    drops: list = []
    collects: list = []

    async def adrop(entry) -> None:
        drops.append(entry)

    monkeypatch.setattr(_conn(consumer), "adrop", adrop)
    monkeypatch.setattr(
        consumer_module,
        "gc",
        SimpleNamespace(collect=lambda: collects.append(1)),
    )
    # Окно закрытия: ``aclose`` потребителя или сервиса уже отказал в
    # работе (флаг ставится первым), а сессии ещё не закрыты — их закроет
    # он сам.
    if closing == "consumer":
        _conn(consumer).closed = True
    else:
        consumer._service._stopped = True

    await _end_turn(consumer)

    assert drops == []
    assert collects == []
    assert alive(pid)


def _spy_lists(monkeypatch) -> list[str]:
    """Каждое обращение потребителя за списком; транспорт настоящий."""
    calls: list[str] = []
    real = connections_module.alist_raw_tools

    async def counting(server_key, server_cfg, *, timeout):
        calls.append(server_key)
        return await real(server_key, server_cfg, timeout=timeout)

    monkeypatch.setattr(connections_module, "alist_raw_tools", counting)
    return calls


def _spy_trace_sent(monkeypatch) -> list:
    """Заголовок trace каждого вызова транспорта; вызов настоящий."""
    sent: list = []
    real = executor_module.acall_mcp_tool

    async def spying(**kwargs):
        cfg = kwargs["servers_cfg"]["net"]
        sent.append((cfg.get("headers") or {}).get("trace_id"))
        return await real(**kwargs)

    monkeypatch.setattr(executor_module, "acall_mcp_tool", spying)
    return sent


async def test_one_off_trace_server_keeps_connection(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    lists = _spy_lists(monkeypatch)
    sent = _spy_trace_sent(monkeypatch)
    async with stream_server(tmp_path, "stream_http") as (network, control):
        # Сетевой сервер без постоянной сессии: trace — в каждом запросе.
        server = {**network, "trace_header_enabled": True}
        settings = _settings({"net": server}, _TRACE)
        consumer = _consumer(mcp_reaper, FakeClock())
        await _ensure(consumer, settings)
        entry = consumer.servers()["net"]
        assert lists == ["net"]

        for _turn in range(3):
            await _end_turn(consumer)
            assert consumer.status_of(entry).state == STATE_CONNECTED
            await asyncio.wait_for(consumer.astart_turn(), 1)
            assert _conn(consumer).attempt_of(entry) == (True, None)
            out = await _call(
                consumer, settings, mcp_tool_full_name("net", "action")
            )

        assert lists == ["net"]
        assert sent == [_TRACE] * 3
        assert launches(control) == [out["pid"]]
