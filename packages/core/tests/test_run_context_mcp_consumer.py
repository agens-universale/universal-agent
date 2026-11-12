"""Потребитель сервиса MCP входит в сборку контекста (спека §5.2, §5.13).

Сборка с ``mcp_consumer`` берёт срез тулов у потребителя, вызовы идут его
исполнителем, сессии живут под его владельцем и не закрываются ни концом
хода, ни пересборкой, ни упавшей или отменённой сборкой. Без потребителя
сборка прежняя. Исполнитель, цикл и тулы настоящие; скриптуется только
ответ модели.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
from types import SimpleNamespace

import pytest
from gc_core import run_context
from gc_core.agent_executor import AgentExecutor
from gc_core.config_modules.mcp import McpServerConfig
from gc_core.plugins.snapshot import PluginSnapshot
from gc_core.run_context import acreate_run_context
from gc_core.tools.connect_mcp import agent_tools
from gc_core.tools.connect_mcp.consumer_status import STATE_CONNECTING
from gc_core.tools.connect_mcp.consumer_view import McpToolsView
from gc_core.tools.connect_mcp.registry import (
    ToolsCache,
    mcp_tool_full_name,
)
from gc_core.tools.connect_mcp.service import McpService
from gc_core.utils.logging_module import release_run_logger
from gc_testkit.mcp import (
    alive,
    launches,
    seed_catalog,
    set_behavior,
    stdio_cfg,
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


def _echo_turn(call_id: str) -> list[dict]:
    """Ходы модели: вызвать ``echo``, затем закончить ход."""
    return [
        assistant_tool_call(_ECHO, {"text": call_id}, f"echo-{call_id}"),
        assistant_tool_call("turn_finish", _FINISH_ARGS, f"fin-{call_id}"),
    ]


async def _build(settings, **kwargs):
    return await asyncio.wait_for(
        acreate_run_context(settings, **kwargs), _CEILING_SEC
    )


async def _turn(executor: AgentExecutor, thread_id: str) -> str:
    return await asyncio.wait_for(
        executor.arun(
            ["Вызови тул"],
            thread_id=thread_id,
            system="cli",
            start_new_thread=True,
            message_ids=[f"in-{thread_id}"],
        ),
        _CEILING_SEC,
    )


def _tool_replies(settings, thread_id: str) -> list[str]:
    """Тексты ответов тула ``echo`` в треде (вывод за его вызовом)."""
    replies: list[str] = []
    called = None
    for row in run_thread_dump(settings, "cli", thread_id):
        payload = row["payload"]
        if payload.get("type") == "function_call":
            called = payload.get("name")
        elif payload.get("type") == "function_call_output":
            if called == _ECHO:
                replies.append(payload["output"])
            called = None
    return replies


def _pid_of(reply: str) -> int:
    """pid процесса сервера из ответа ``echo``."""
    (content,) = json.loads(reply)["result"]["content"]
    return json.loads(content["text"])["pid"]


def _open_consumer(reaper):
    """Потребитель сервиса с политикой «на старте»; закроет уборка."""
    service = McpService(connect_on_start=True)
    return reaper.track(service.open_consumer("окно"))


async def test_two_turns_and_two_contexts_use_one_session(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    settings = _cfg(tmp_path, monkeypatch, {"srv": stdio_cfg(control)})
    monkeypatch.chdir(tmp_path)
    backend = install(
        monkeypatch,
        ScriptedBackend(_echo_turn("1") + _echo_turn("2") + _echo_turn("3")),
    )
    consumer = _open_consumer(mcp_reaper)

    first = await _build(settings, mcp_consumer=consumer)
    try:
        executor = AgentExecutor(first)
        await _turn(executor, "t1")
        await _turn(executor, "t2")
    finally:
        release_run_logger(first.logger)
    second = await _build(settings, mcp_consumer=consumer)
    try:
        await _turn(AgentExecutor(second), "t3")
    finally:
        release_run_logger(second.logger)

    backend.assert_clean()
    replies = [
        reply
        for thread in ("t1", "t2", "t3")
        for reply in _tool_replies(settings, thread)
    ]
    pids = {_pid_of(reply) for reply in replies}
    assert len(replies) == 3
    assert pids == set(launches(control))
    assert len(launches(control)) == 1


async def test_tools_come_from_catalog_before_connect(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    set_behavior(control, start_delay=1.5)
    settings = _cfg(tmp_path, monkeypatch, {"srv": stdio_cfg(control)})
    seed_catalog(settings, "srv", ["echo", "nap"])
    consumer = _open_consumer(mcp_reaper)

    ctx = await _build(settings, mcp_consumer=consumer)
    release_run_logger(ctx.logger)

    assert _ECHO in ctx.tools_data
    assert ctx.mcp_status["srv"]["status"] == STATE_CONNECTING


async def _session_pid(reaper, tmp_path, monkeypatch, servers):
    """Потребитель, собравший первый контекст: pid сессии ``srv``."""
    settings = _cfg(tmp_path, monkeypatch, servers)
    consumer = _open_consumer(reaper)
    ctx = await _build(settings, mcp_consumer=consumer)
    release_run_logger(ctx.logger)
    (pid,) = launches(tmp_path / "srv.json")
    assert alive(pid)
    return consumer, settings, pid


async def test_failed_build_keeps_consumer_sessions(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    consumer, settings, pid = await _session_pid(
        mcp_reaper,
        tmp_path,
        monkeypatch,
        {"srv": stdio_cfg(tmp_path / "srv.json")},
    )

    def broken(*_args, **_kwargs):
        raise RuntimeError("досборка упала")

    monkeypatch.setattr(run_context, "_finish_run_context", broken)
    with pytest.raises(RuntimeError, match="досборка упала"):
        await _build(settings, mcp_consumer=consumer)

    assert alive(pid)


async def test_cancelled_build_keeps_consumer_sessions(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    slow = tmp_path / "slow.json"
    servers = {"srv": stdio_cfg(tmp_path / "srv.json")}
    consumer, _settings, pid = await _session_pid(
        mcp_reaper, tmp_path, monkeypatch, servers
    )
    set_behavior(slow, start_delay=60)
    settings = _cfg(
        tmp_path, monkeypatch, {**servers, "slow": stdio_cfg(slow)}
    )

    # Сервер без каталога: сверка второй сборки ждёт его подключения.
    build = asyncio.ensure_future(
        acreate_run_context(settings, mcp_consumer=consumer)
    )
    await wait_for(lambda: bool(launches(slow)), _CEILING_SEC)
    build.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(build, _CEILING_SEC)

    assert alive(pid)


async def test_consumer_with_tools_cache_is_rejected(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    settings = _cfg(tmp_path, monkeypatch, {})
    consumer = _open_consumer(mcp_reaper)

    with pytest.raises(ValueError, match="вместе не задаются"):
        await _build(settings, mcp_consumer=consumer, tools_cache=ToolsCache())

    # Отклонённая сборка номер не сожгла.
    assert consumer.next_build() == 1


async def test_store_check_failure_does_not_take_build_number(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    settings = _cfg(tmp_path, monkeypatch, {})
    consumer = _open_consumer(mcp_reaper)

    with pytest.raises(TypeError, match="agent_store"):
        await _build(settings, mcp_consumer=consumer, agent_store=object())

    assert consumer.next_build() == 1


async def test_consumer_set_follows_build_runtime(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    """Сверка идёт настройками этой сборки: сервер плагина из явного
    ``runtime=`` попадает и в набор потребителя, и в тулы контекста."""
    settings = _cfg(tmp_path, monkeypatch, {})
    server = McpServerConfig.model_validate(stdio_cfg(tmp_path / "srv.json"))
    runtime = dataclasses.replace(
        settings.runtime,
        plugins=PluginSnapshot(mcp_servers={"srv": server}),
    )
    consumer = _open_consumer(mcp_reaper)

    ctx = await _build(settings, runtime=runtime, mcp_consumer=consumer)
    release_run_logger(ctx.logger)

    assert _ECHO in ctx.tools_data
    assert list(consumer.servers()) == ["srv"]


async def test_view_failure_does_not_break_build(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    settings = _cfg(
        tmp_path, monkeypatch, {"srv": stdio_cfg(tmp_path / "srv.json")}
    )
    consumer = _open_consumer(mcp_reaper)

    async def broken_view(*_args, **_kwargs):
        raise RuntimeError("срез упал")

    monkeypatch.setattr(consumer, "aview", broken_view)
    ctx = await _build(settings, mcp_consumer=consumer)
    release_run_logger(ctx.logger)

    assert not [name for name in ctx.tools_data if name.startswith("mcp_")]
    assert ctx.mcp_status == {}


class _SandboxBackend:
    """Бэкенд-песочница: прежний путь предупредил бы о кэше без неё."""

    is_sandbox = True


async def test_consumer_source_skips_cache_preparation(caplog):
    """Источник со срезом не проходит подготовку кэша: у потребителя нет
    ни ``rebind``, ни ``os_backend``, и ложного предупреждения о
    дискавери мимо песочницы быть не должно."""
    settings = make_settings(
        {
            "mcp.servers": {
                "srv": {
                    "transport": "stdio",
                    "command": "absent",
                    "execute_on": "os_backend",
                }
            }
        }
    )
    sandbox = _SandboxBackend()
    seen: list = []

    async def caller(**_kwargs):
        return None

    async def aview(view_settings, *, os_backend=None):
        seen.append((view_settings, os_backend))
        entry = {
            "tool": {"name": "echo", "description": "эхо"},
            "metadata": {"mcp_server_key": "srv"},
        }
        return McpToolsView(
            {_ECHO: entry},
            {"srv": {"status": "connected", "tool_count": 1}},
            {"srv": {"transport": "stdio"}},
            caller,
        )

    source = SimpleNamespace(aview=aview)
    with caplog.at_level(logging.WARNING):
        tools, status = await agent_tools.aget_mcp_tools_data_for_config(
            settings, tools_cache=source, os_backend=sandbox
        )

    assert seen == [(settings, sandbox)]
    assert list(tools) == [_ECHO]
    assert status == {"srv": {"status": "connected", "tool_count": 1}}
    assert tools[_ECHO]["tool_fabric"]()._caller is caller
    assert "without os_backend" not in caplog.text


async def test_turn_end_uses_cleanup_port(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    consumer, settings, pid = await _session_pid(
        mcp_reaper,
        tmp_path,
        monkeypatch,
        {"srv": stdio_cfg(tmp_path / "srv.json")},
    )

    ctx = await _build(settings, mcp_consumer=consumer)
    release_run_logger(ctx.logger)

    assert ctx.mcp_cleanup == consumer.aend_turn
    assert ctx.mcp_session_owner == consumer.owner
    assert ctx.tools_cache is consumer
    await asyncio.wait_for(consumer.aend_turn(), _CEILING_SEC)
    assert alive(pid)


async def test_closed_consumer_leaves_running_context_harmless(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    settings = _cfg(tmp_path, monkeypatch, {"srv": stdio_cfg(control)})
    monkeypatch.chdir(tmp_path)
    backend = install(monkeypatch, ScriptedBackend(_echo_turn("1")))
    consumer = _open_consumer(mcp_reaper)
    ctx = await _build(settings, mcp_consumer=consumer)

    await asyncio.wait_for(consumer.aclose(), _CEILING_SEC)
    try:
        result = await _turn(AgentExecutor(ctx), "t1")
    finally:
        release_run_logger(ctx.logger)

    backend.assert_clean()
    (reply,) = _tool_replies(settings, "t1")
    assert "остановлен" in reply
    assert json.loads(result)["task_report"] == "Готово"
    # Исполнитель потребителя не поднимает сервер заново.
    assert len(launches(control)) == 1


async def test_context_without_consumer_is_unchanged(
    mcp_home, tmp_path, monkeypatch
):
    settings = _cfg(tmp_path, monkeypatch, {})

    plain = await _build(settings)
    release_run_logger(plain.logger)
    cache = ToolsCache()
    cached = await _build(settings, tools_cache=cache)
    release_run_logger(cached.logger)

    assert plain.mcp_cleanup is None
    assert cached.mcp_cleanup is None
    assert plain.mcp_session_owner.startswith("ctx-")
    assert cached.mcp_session_owner.startswith("ctx-")
    assert plain.mcp_session_owner != cached.mcp_session_owner
    assert cached.tools_cache is cache
