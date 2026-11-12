"""``AsyncAgent`` с сервисом MCP (спека §5.1, §5.2).

Агент, получивший ``mcp_service``, сам открывает у сервиса своего
потребителя: сессии живут между ходами (``run``) и закрываются вместе с
агентом (``aclose``) или отказом его собственной сборки. Потребитель,
переданный хостом (``mcp_consumer=``), — хоста: агент его не закрывает.
Процессы тестовых серверов настоящие; скриптуется только ответ модели.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import pytest
from gc_core import run_context as run_context_module
from gc_core.aio.agent import AsyncAgent
from gc_core.tools.connect_mcp.consumer import McpConsumer
from gc_core.tools.connect_mcp.registry import ToolsCache, mcp_tool_full_name
from gc_core.tools.connect_mcp.service import McpService
from gc_testkit.mcp import (
    exits,
    launches,
    set_behavior,
    stdio_cfg,
    wait_for,
)
from gc_testkit.mcp.fixtures import mcp_home, mcp_owner, mcp_reaper
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
_ECHO = mcp_tool_full_name("srv", "echo")
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


def _cfg(tmp_path, monkeypatch, servers: dict):
    return parity_config(
        tmp_path, monkeypatch, **{**_CONFIG, "mcp.servers": servers}
    )


async def _create(reaper, settings, **kwargs) -> AsyncAgent:
    """Агент под потолком; закроет уборка, если тест не дошёл до
    ``aclose``."""
    agent = await asyncio.wait_for(
        AsyncAgent.create(settings, **kwargs), _CEILING_SEC
    )
    return reaper.track(agent)


def _count_opens(monkeypatch, service: McpService) -> list[str]:
    """Метки потребителей, открытых у ``service`` после этого вызова."""
    opened: list[str] = []
    real = service.open_consumer

    def counted(label: str):
        opened.append(label)
        return real(label)

    monkeypatch.setattr(service, "open_consumer", counted)
    return opened


def _failing_consumer_close(monkeypatch) -> None:
    """Закрытие потребителя доходит до конца и затем бросает."""
    real = McpConsumer.aclose

    async def failing(self) -> None:
        await real(self)
        raise RuntimeError("закрытие потребителя упало")

    monkeypatch.setattr(McpConsumer, "aclose", failing)


def _echo_turn(call_id: str) -> list[dict]:
    """Ходы модели: вызвать ``echo``, затем закончить ход."""
    return [
        assistant_tool_call(_ECHO, {"text": call_id}, f"echo-{call_id}"),
        assistant_tool_call("turn_finish", _FINISH_ARGS, f"fin-{call_id}"),
    ]


def _echo_pids(settings, thread_id: str) -> list[int]:
    """pid процесса сервера в ответах ``echo`` треда."""
    pids: list[int] = []
    called = None
    for row in run_thread_dump(settings, "cli", thread_id):
        payload = row["payload"]
        if payload.get("type") == "function_call":
            called = payload.get("name")
        elif payload.get("type") == "function_call_output":
            if called == _ECHO:
                (content,) = json.loads(payload["output"])["result"]["content"]
                pids.append(json.loads(content["text"])["pid"])
            called = None
    return pids


async def _echo_pid(agent: AsyncAgent) -> int:
    """Вызов ``echo`` обёрткой из набора агента — той, что вызовет цикл."""
    tools_data = agent.executor._ctx.tools_data
    envelope = await asyncio.wait_for(
        tools_data[_ECHO]["tool_fabric"]().acall(text="эхо"), _CEILING_SEC
    )
    (content,) = json.loads(envelope)["result"]["content"]
    return json.loads(content["text"])["pid"]


async def test_sessions_live_between_runs_and_close_with_agent(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    settings = _cfg(tmp_path, monkeypatch, {"srv": stdio_cfg(control)})
    monkeypatch.chdir(tmp_path)
    backend = install(
        monkeypatch, ScriptedBackend(_echo_turn("1") + _echo_turn("2"))
    )
    service = McpService(connect_on_start=True)
    agent = await _create(mcp_reaper, settings, mcp_service=service)

    for thread_id in ("t1", "t2"):
        await asyncio.wait_for(
            agent.run(
                "Вызови тул",
                thread_id=thread_id,
                system="cli",
                start_new_thread=True,
            ),
            _CEILING_SEC,
        )
    backend.assert_clean()
    (first,) = _echo_pids(settings, "t1")
    (second,) = _echo_pids(settings, "t2")

    assert first == second
    assert launches(control) == [first]
    assert exits(control) == []

    await asyncio.wait_for(agent.aclose(), _CEILING_SEC)

    await wait_for(lambda: first in exits(control), _CEILING_SEC)
    assert agent._mcp_consumer.closed
    assert not service._consumers


async def test_failed_create_closes_its_consumer(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    settings = _cfg(tmp_path, monkeypatch, {"srv": stdio_cfg(control)})
    service = McpService(connect_on_start=True)

    def broken(*_args, **_kwargs):
        raise RuntimeError("досборка упала")

    monkeypatch.setattr(run_context_module, "_finish_run_context", broken)
    with pytest.raises(RuntimeError, match="досборка упала"):
        await _create(mcp_reaper, settings, mcp_service=service)

    assert not service._consumers
    # Сверка набора успела поднять сессию — закрытие потребителя её гасит.
    (pid,) = launches(control)
    await wait_for(lambda: pid in exits(control), _CEILING_SEC)


async def test_cancelled_create_closes_its_consumer(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    slow = tmp_path / "slow.json"
    set_behavior(slow, start_delay=60)
    settings = _cfg(tmp_path, monkeypatch, {"slow": stdio_cfg(slow)})
    service = McpService(connect_on_start=True)

    # Сервер без каталога: сверка ждёт его подключения.
    create = asyncio.ensure_future(
        AsyncAgent.create(settings, mcp_service=service)
    )
    await wait_for(lambda: bool(launches(slow)), _CEILING_SEC)
    create.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(create, _CEILING_SEC)

    assert not service._consumers


async def test_failed_close_keeps_build_error(
    mcp_home, tmp_path, monkeypatch, caplog, mcp_reaper, mcp_owner
):
    settings = _cfg(tmp_path, monkeypatch, {})
    service = McpService(connect_on_start=True)
    _failing_consumer_close(monkeypatch)

    def broken(*_args, **_kwargs):
        raise RuntimeError("досборка упала")

    monkeypatch.setattr(run_context_module, "_finish_run_context", broken)
    with caplog.at_level(logging.WARNING, logger="gc_core.aio.agent"):
        with pytest.raises(RuntimeError, match="досборка упала"):
            await _create(mcp_reaper, settings, mcp_service=service)

    assert "закрытие потребителя упало" in caplog.text
    assert not service._consumers


async def test_failed_close_keeps_cancellation(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    slow = tmp_path / "slow.json"
    set_behavior(slow, start_delay=60)
    settings = _cfg(tmp_path, monkeypatch, {"slow": stdio_cfg(slow)})
    service = McpService(connect_on_start=True)
    _failing_consumer_close(monkeypatch)

    create = asyncio.ensure_future(
        AsyncAgent.create(settings, mcp_service=service)
    )
    await wait_for(lambda: bool(launches(slow)), _CEILING_SEC)
    create.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(create, _CEILING_SEC)

    assert create.cancelled()
    assert not service._consumers


async def test_service_with_durable_is_rejected(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    settings = _cfg(tmp_path, monkeypatch, {})
    service = McpService(connect_on_start=True)
    opened = _count_opens(monkeypatch, service)

    with pytest.raises(TypeError, match="durable="):
        await _create(
            mcp_reaper, settings, durable=object(), mcp_service=service
        )

    assert opened == []
    assert not service._consumers


async def test_service_on_another_loop_is_rejected(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    settings = _cfg(tmp_path, monkeypatch, {"srv": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    # Обращение с цикла теста привязывает сервис к нему.
    service.entry("srv", stdio_cfg(control), discovery_timeout=30)

    def on_other_loop() -> BaseException | None:
        try:
            asyncio.run(AsyncAgent.create(settings, mcp_service=service))
        except BaseException as exc:
            return exc
        return None

    error = await asyncio.wait_for(
        asyncio.to_thread(on_other_loop), _CEILING_SEC
    )

    assert isinstance(error, RuntimeError)
    assert "другом цикле" in str(error)
    assert not service._consumers
    assert launches(control) == []


async def test_service_with_tools_cache_is_rejected(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    settings = _cfg(tmp_path, monkeypatch, {})
    service = McpService(connect_on_start=True)
    opened = _count_opens(monkeypatch, service)

    with pytest.raises(ValueError, match="tools_cache"):
        await _create(
            mcp_reaper,
            settings,
            mcp_service=service,
            tools_cache=ToolsCache(),
        )

    assert opened == []
    assert not service._consumers


async def test_service_with_host_consumer_is_rejected(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    settings = _cfg(tmp_path, monkeypatch, {})
    service = McpService(connect_on_start=True)
    consumer = mcp_reaper.track(service.open_consumer("хост"))
    opened = _count_opens(monkeypatch, service)

    with pytest.raises(ValueError, match="mcp_consumer"):
        await _create(
            mcp_reaper, settings, mcp_service=service, mcp_consumer=consumer
        )

    # Отказ не открыл агенту своего потребителя и не тронул чужого.
    assert opened == []
    assert service._consumers == {consumer}
    assert not consumer.closed


@pytest.mark.parametrize("name", ["tools_cache", "mcp_consumer"])
async def test_service_with_empty_source_is_accepted(
    mcp_home, tmp_path, monkeypatch, name, mcp_reaper, mcp_owner
):
    """Явный ``None`` по старой сигнатуре фабрики хоста безвреден."""
    control = tmp_path / "srv.json"
    settings = _cfg(tmp_path, monkeypatch, {"srv": stdio_cfg(control)})
    service = McpService(connect_on_start=True)

    agent = await _create(
        mcp_reaper, settings, mcp_service=service, **{name: None}
    )

    assert service._consumers == {agent._mcp_consumer}
    assert agent.executor._ctx.tools_cache is agent._mcp_consumer
    assert await _echo_pid(agent) == launches(control)[0]


async def test_two_agents_share_entries_not_sessions(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    settings = _cfg(tmp_path, monkeypatch, {"srv": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    first = await _create(mcp_reaper, settings, mcp_service=service)
    second = await _create(mcp_reaper, settings, mcp_service=service)

    first_pid = await _echo_pid(first)
    second_pid = await _echo_pid(second)

    assert first_pid != second_pid
    # По сессии на агента; запись сервера одна на сервис.
    assert len(launches(control)) == 2
    assert len(service._entries) == 1
    assert first._mcp_consumer is not second._mcp_consumer


async def test_host_consumer_stays_open_after_agent_close(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    settings = _cfg(tmp_path, monkeypatch, {"srv": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    consumer = mcp_reaper.track(service.open_consumer("хост"))
    agent = await _create(mcp_reaper, settings, mcp_consumer=consumer)
    (pid,) = launches(control)

    await asyncio.wait_for(agent.aclose(), _CEILING_SEC)

    assert agent._mcp_consumer is None
    assert not consumer.closed
    assert service._consumers == {consumer}
    assert exits(control) == []


async def test_agent_close_after_service_stop_and_twice(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    settings = _cfg(tmp_path, monkeypatch, {"srv": stdio_cfg(control)})
    service = McpService(connect_on_start=True)
    agent = await _create(mcp_reaper, settings, mcp_service=service)
    (pid,) = launches(control)
    # Закрытие сервиса закрывает и потребителя агента.
    await asyncio.wait_for(service.aclose(), _CEILING_SEC)
    assert pid in exits(control)

    await asyncio.wait_for(agent.aclose(), _CEILING_SEC)
    await asyncio.wait_for(agent.aclose(), _CEILING_SEC)

    assert agent._mcp_consumer.closed
    assert not service._consumers


async def test_agent_without_service_unchanged(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    settings = _cfg(tmp_path, monkeypatch, {})

    agent = await _create(mcp_reaper, settings)

    assert agent._mcp_consumer is None
