"""Субагенты и серверы бэкенда под потребителем сервиса MCP.

Спека §5.11: субагент получает закреплённый срез родителя через
унаследованный ``ctx.tools_cache`` — без дискавери и без сверки набора;
его вызовы идут по сессиям потребителя родителя, и конец его хода эти
сессии не закрывает. Спека §5.10 и §5.6: сервер ``execute_on: os_backend``
исполняется бэкендом сборки, а пересборка набора на входе в ход не гонит
его обнаружение заново. Процессы тестовых серверов настоящие; модель и
изолированный бэкенд — подставные.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os

import pytest
from gc_core import run_context as run_context_module
from gc_core.agent_edits.tracking import TrackingFsBackend
from gc_core.agent_executor import AgentExecutor
from gc_core.backends.local import LocalFsBackend
from gc_core.mcp_session_owner import use_owner
from gc_core.run_context import acreate_run_context
from gc_core.tools import abuild_tools_data
from gc_core.tools.connect_mcp import catalog
from gc_core.tools.connect_mcp.registry import mcp_tool_full_name
from gc_core.tools.connect_mcp.server_identity import SESSION_KEY_FIELD
from gc_core.tools.connect_mcp.service import McpService
from gc_core.tools.subagents.runner import SubagentRunner
from gc_core.utils.logging_module import release_run_logger
from gc_testkit.mcp import (
    BACKEND_ECHO_TEXT,
    IsolatedBackend,
    alive,
    events,
    exits,
    launches,
    seed_catalog,
    stdio_cfg,
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
_WORKER = "subagent_worker"
_ECHO = mcp_tool_full_name("srv", "echo")
_EXTRA = mcp_tool_full_name("srv", "extra")
_A_ECHO = mcp_tool_full_name("a", "echo")
_B_ECHO = mcp_tool_full_name("b", "echo")
_BK_ECHO = mcp_tool_full_name("bk", "echo")
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


def _cfg(tmp_path, monkeypatch, servers: dict, **extra):
    return parity_config(
        tmp_path, monkeypatch, **{**_CONFIG, **extra, "mcp.servers": servers}
    )


def _open_consumer(reaper, *, on_start: bool = True):
    """Потребитель сервиса; закроет уборка."""
    service = McpService(connect_on_start=on_start)
    return reaper.track(service.open_consumer("окно"))


@contextlib.asynccontextmanager
async def _context(settings, **kwargs):
    ctx = await asyncio.wait_for(
        acreate_run_context(settings, **kwargs), _CEILING_SEC
    )
    try:
        yield ctx
    finally:
        release_run_logger(ctx.logger)


def _runner(ctx) -> SubagentRunner:
    """Раннер субагента родителя ``ctx`` — как его создаёт вызов тула."""
    runner = SubagentRunner(ctx, None)
    runner._active_tool_name = _WORKER
    return runner


async def _child_tools(runner: SubagentRunner, ctx) -> dict:
    """Сборка тулов ребёнка тем же вызовом, что в раннере."""
    tools_data, *_rest = await asyncio.wait_for(
        abuild_tools_data(
            runner._build_child_config(),
            os_backend=ctx.os_backend,
            skills_registry=ctx.skills_registry,
            tools_cache=ctx.tools_cache,
        ),
        _CEILING_SEC,
    )
    return tools_data


def _mcp_names(tools_data: dict) -> set[str]:
    return {name for name in tools_data if name.startswith("mcp_")}


def _server_answer(reply: str) -> dict:
    """Ответ тестового сервера из конверта результата тула."""
    (content,) = json.loads(reply)["result"]["content"]
    return json.loads(content["text"])


def _wrapper(tools_data: dict, tool: str = _ECHO):
    """Обёртка тула из набора — та, что вызовет цикл агента."""
    return tools_data[tool]["tool_fabric"]()


async def _echo_pid(tools_data: dict, owner: str, tool: str = _ECHO) -> int:
    """Вызов ``echo`` обёрткой из набора под владельцем ``owner``: pid."""
    with use_owner(owner):
        envelope = await asyncio.wait_for(
            _wrapper(tools_data, tool).acall(text="эхо"), _CEILING_SEC
        )
    return _server_answer(envelope)["pid"]


async def test_child_uses_parent_session_without_discovery(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    control = tmp_path / "srv.json"
    settings = _cfg(tmp_path, monkeypatch, {"srv": stdio_cfg(control)})
    consumer = _open_consumer(mcp_reaper)

    async with _context(settings, mcp_consumer=consumer) as ctx:
        owner = ctx.mcp_session_owner
        parent_pid = await _echo_pid(ctx.tools_data, owner)
        runner = _runner(ctx)
        await _child_tools(runner, ctx)
        child = await _child_tools(runner, ctx)
        child_pid = await _echo_pid(child, owner)
        # Владелец вокруг вызова не решает: вызов идёт по сессии
        # потребителя родителя, а не по сессии текущего владельца.
        foreign_pid = await _echo_pid(child, "чужой")

    assert child_pid == parent_pid
    assert foreign_pid == parent_pid
    assert len(launches(control)) == 1
    # Обёртка ребёнка вызывает исполнителем потребителя родителя.
    assert _wrapper(child)._caller == consumer._executor.acall


async def test_child_tool_lists_agree(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    settings = _cfg(
        tmp_path, monkeypatch, {"srv": stdio_cfg(tmp_path / "srv.json")}
    )
    consumer = _open_consumer(mcp_reaper)

    async with _context(settings, mcp_consumer=consumer) as ctx:
        runner = _runner(ctx)
        first = await _child_tools(runner, ctx)
        entry = consumer.servers()["srv"]
        consumer._service.record_success(entry, [{"name": "extra"}])
        second = await _child_tools(runner, ctx)

    assert _ECHO in first
    assert _mcp_names(first) == _mcp_names(second)


async def test_child_mcp_allow_filter_applies(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    servers = {
        "a": stdio_cfg(tmp_path / "a.json"),
        "b": stdio_cfg(tmp_path / "b.json"),
    }
    settings = _cfg(tmp_path, monkeypatch, servers)
    consumer = _open_consumer(mcp_reaper)

    async with _context(settings, mcp_consumer=consumer) as ctx:
        runner = _runner(ctx)
        # Область инструментов из файла субагента плагина.
        scope = {
            "subagent_tools_allow": ("fs_read_file",),
            "subagent_tools_stop": (),
            "subagent_mcp_allow": (("a", None),),
        }
        monkeypatch.setattr(runner, "_child_tool_scope", lambda: scope)
        assert runner._build_child_runtime().subagent_mcp_allow == (
            ("a", None),
        )
        child = await _child_tools(runner, ctx)

    assert {_A_ECHO, _B_ECHO} <= _mcp_names(ctx.tools_data)
    assert _A_ECHO in child
    assert {name.split("_")[1] for name in _mcp_names(child)} == {"a"}


def _echo_turn(call_id: str) -> list[dict]:
    """Ход модели: вызвать ``echo``."""
    return [assistant_tool_call(_ECHO, {"text": call_id}, f"echo-{call_id}")]


def _finish(call_id: str) -> dict:
    return assistant_tool_call("turn_finish", _FINISH_ARGS, f"fin-{call_id}")


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


def _tool_outputs(settings, thread_id: str, tool: str) -> list[str]:
    """Тексты ответов тула ``tool`` в треде (вывод за его вызовом)."""
    outputs: list[str] = []
    called = None
    for row in run_thread_dump(settings, "cli", thread_id):
        payload = row["payload"]
        if payload.get("type") == "function_call":
            called = payload.get("name")
        elif payload.get("type") == "function_call_output":
            if called == tool:
                outputs.append(payload["output"])
            called = None
    return outputs


def _calls(control) -> int:
    """Сколько вызовов тулов принял сервер (все его процессы)."""
    return sum(
        1 for line in events(control) if line.startswith("req tools/call ")
    )


def _spy_child_ctx(monkeypatch) -> list:
    """Контексты ребёнка, собранные раннером."""
    seen: list = []
    real = SubagentRunner._build_child_ctx

    async def spy(self, tool_name):
        child_ctx = await real(self, tool_name)
        seen.append(child_ctx)
        return child_ctx

    monkeypatch.setattr(SubagentRunner, "_build_child_ctx", spy)
    return seen


async def test_subagent_turn_end_keeps_parent_sessions(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    """Ход субагента вызывает ``echo`` и заканчивается; сессия потребителя
    родителя переживает конец хода ребёнка, и следующий вызов родителя
    идёт по тому же процессу."""
    control = tmp_path / "srv.json"
    settings = _cfg(
        tmp_path,
        monkeypatch,
        {"srv": stdio_cfg(control)},
        **{"agent.use_local_subagents": True},
    )
    monkeypatch.chdir(tmp_path)
    worker_args = {"task": "Вызови тул", "reason": "проверка"}
    backend = install(
        monkeypatch,
        ScriptedBackend(
            [
                *_echo_turn("1"),
                assistant_tool_call(_WORKER, worker_args, "worker-1"),
                *_echo_turn("child"),
                _finish("child"),
                *_echo_turn("2"),
                _finish("parent"),
            ]
        ),
    )
    children = _spy_child_ctx(monkeypatch)
    consumer = _open_consumer(mcp_reaper)

    async with _context(settings, mcp_consumer=consumer) as ctx:
        servers = consumer.servers()
        await _turn(AgentExecutor(ctx), "t1")

    backend.assert_clean()
    (child_ctx,) = children
    assert child_ctx.tools_cache is consumer
    # Владельца сессий потребителя ребёнок не получает: уборка по его
    # владельцу сессий родителя не задела бы.
    assert child_ctx.mcp_session_owner != consumer.owner
    assert _ECHO in child_ctx.tools_data
    replies = _tool_outputs(settings, "t1", _ECHO)
    (pid,) = launches(control)
    assert [_server_answer(reply)["pid"] for reply in replies] == [pid, pid]
    assert _calls(control) == 3
    assert exits(control) == []
    assert alive(pid)
    # Набор потребителя ходом ребёнка не сверялся: записи те же.
    assert consumer.servers() == servers
    assert consumer.servers()["srv"] is servers["srv"]


async def test_child_builds_keep_parent_pin_and_signal(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    """Сборки тулов ребёнка идут срезом без сверки: набор и закреплённый
    срез родителя не сдвигаются, вход в ход родителя после
    них не получает ложного сигнала и не теряет настоящий."""
    settings = _cfg(
        tmp_path, monkeypatch, {"srv": stdio_cfg(tmp_path / "srv.json")}
    )
    consumer = _open_consumer(mcp_reaper)

    async with _context(settings, mcp_consumer=consumer) as ctx:
        runner = _runner(ctx)
        servers = consumer.servers()
        await _child_tools(runner, ctx)
        quiet = await asyncio.wait_for(consumer.astart_turn(), 1)
        pinned = dict(consumer._pinned)
        entry = servers["srv"]
        consumer._service.record_success(entry, [{"name": "extra"}])
        child = await _child_tools(runner, ctx)
        servers_after_child = consumer.servers()
        pinned_after_child = dict(consumer._pinned)
        changed = await asyncio.wait_for(consumer.astart_turn(), 1)

    assert quiet is False
    assert _ECHO in child
    assert _EXTRA not in child
    # Набор не сверялся: те же записи; закреплённый срез тот же.
    assert servers_after_child == servers
    assert servers_after_child["srv"] is entry
    assert pinned_after_child == pinned
    assert changed is True


def _backend_server(control) -> dict:
    return {**stdio_cfg(control), "execute_on": "os_backend"}


def _tool_names(payload: dict) -> set[str]:
    """Имена тулов в запросе к модели."""
    return {spec["function"]["name"] for spec in payload.get("tools", [])}


async def test_backend_side_server_end_to_end(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    control = tmp_path / "bk.json"
    settings = _cfg(tmp_path, monkeypatch, {"bk": _backend_server(control)})
    monkeypatch.chdir(tmp_path)
    model = install(
        monkeypatch,
        ScriptedBackend(
            [
                assistant_tool_call(_BK_ECHO, {"text": "1"}, "bk-1"),
                _finish("1"),
            ]
        ),
    )
    os_backend = IsolatedBackend()
    consumer = _open_consumer(mcp_reaper)

    async with _context(
        settings, mcp_consumer=consumer, os_backend=os_backend
    ) as ctx:
        await _turn(AgentExecutor(ctx), "t1")

    model.assert_clean()
    (reply,) = _tool_outputs(settings, "t1", _BK_ECHO)
    assert _server_answer(reply)["text"] == BACKEND_ECHO_TEXT
    assert os_backend.methods == ["tools/list", "tools/call"]
    assert consumer.servers() == {}
    assert not catalog._key_dir("bk").exists()
    assert launches(control) == []


async def test_backend_part_survives_refresh_without_discovery(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    """Смешанный набор: агентный сервер (тулы из каталога, подключение при
    первом вызове) и сервер бэкенда. Запись агентного меняет список —
    следующий ход пересобирает набор: обнаружение сервера бэкенда не
    повторяется, у агентного сервера запусков нет."""
    control = tmp_path / "srv.json"
    bk_control = tmp_path / "bk.json"
    settings = _cfg(
        tmp_path,
        monkeypatch,
        {"srv": stdio_cfg(control), "bk": _backend_server(bk_control)},
    )
    seed_catalog(settings, "srv", ["echo"])
    monkeypatch.chdir(tmp_path)
    model = install(monkeypatch, ScriptedBackend([_finish("1"), _finish("2")]))
    os_backend = IsolatedBackend()
    consumer = _open_consumer(mcp_reaper, on_start=False)

    async with _context(
        settings, mcp_consumer=consumer, os_backend=os_backend
    ) as ctx:
        executor = AgentExecutor(ctx)
        await _turn(executor, "t1")
        entry = consumer.servers()["srv"]
        consumer._service.record_success(
            entry, [{"name": "echo"}, {"name": "extra"}]
        )
        await _turn(executor, "t2")

    model.assert_clean()
    assert os_backend.methods == ["tools/list"]
    assert launches(control) == []
    assert launches(bk_control) == []
    first, second = (_tool_names(call) for call in model.calls)
    assert {_ECHO, _BK_ECHO} <= first
    assert _EXTRA not in first
    # Набор пересобран: новый тул агентного сервера и прежняя часть бэкенда.
    assert {_ECHO, _EXTRA, _BK_ECHO} <= second


async def test_child_wrapper_definitions_match_parent(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    """Обёртка ребёнка несёт те же определения сервера, что у родителя:
    ключ сессии с отпечатком, постоянную сессию, trace сборки."""
    server = {**stdio_cfg(tmp_path / "srv.json"), "trace_header_enabled": True}
    settings = _cfg(tmp_path, monkeypatch, {"srv": server}, trace_id="след-1")
    consumer = _open_consumer(mcp_reaper)

    async with _context(settings, mcp_consumer=consumer) as ctx:
        child = await _child_tools(_runner(ctx), ctx)
        parent_wrapper = _wrapper(ctx.tools_data)
        child_wrapper = _wrapper(child)

    parent_cfg = parent_wrapper._servers_cfg["srv"]
    assert child_wrapper._servers_cfg["srv"] == parent_cfg
    assert parent_cfg[SESSION_KEY_FIELD]
    assert parent_cfg["persistent_session"] is True
    assert parent_cfg["env"]["TRACE_ID"] == "след-1"
    assert child_wrapper._timeout == parent_wrapper._timeout
    assert child_wrapper._os_backend is parent_wrapper._os_backend


async def test_child_ctx_takes_backend_part_from_consumer_cache(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    """Сборка контекста ребёнка раннером в смешанном наборе, бэкенд
    родителя под обёрткой записи правок: часть бэкенда — из кэша
    потребителя, нового обнаружения через бэкенд нет."""
    control = tmp_path / "srv.json"
    bk_control = tmp_path / "bk.json"
    settings = _cfg(
        tmp_path,
        monkeypatch,
        {"srv": stdio_cfg(control), "bk": _backend_server(bk_control)},
    )
    raw = IsolatedBackend()
    monkeypatch.setattr(
        run_context_module, "resolve_os_backend", lambda *_a, **_k: raw
    )
    monkeypatch.chdir(tmp_path)
    consumer = _open_consumer(mcp_reaper)

    # TUI: запись правок включена — контекст оборачивает бэкенд.
    async with _context(settings, mcp_consumer=consumer, is_tui=True) as ctx:
        child_ctx = await asyncio.wait_for(
            _runner(ctx)._build_child_ctx(_WORKER), _CEILING_SEC
        )
        release_run_logger(child_ctx.logger)

    assert isinstance(child_ctx.os_backend, TrackingFsBackend)
    assert {_ECHO, _BK_ECHO} <= _mcp_names(child_ctx.tools_data)
    assert raw.methods == ["tools/list"]
    assert len(launches(control)) == 1
    assert launches(bk_control) == []


async def test_backend_server_on_plain_backend_goes_through_service(
    mcp_home, tmp_path, monkeypatch, mcp_reaper, mcp_owner
):
    """``execute_on: os_backend`` на неизолированном бэкенде исполняется
    на стороне агента — через сервис на общих основаниях (спека §5.10)."""
    control = tmp_path / "bk.json"
    settings = _cfg(tmp_path, monkeypatch, {"bk": _backend_server(control)})
    consumer = _open_consumer(mcp_reaper)

    async with _context(
        settings, mcp_consumer=consumer, os_backend=LocalFsBackend()
    ) as ctx:
        owner = ctx.mcp_session_owner
        first = await _echo_pid(ctx.tools_data, owner, _BK_ECHO)
        second = await _echo_pid(ctx.tools_data, owner, _BK_ECHO)
        wrapper = _wrapper(ctx.tools_data, _BK_ECHO)

    assert list(consumer.servers()) == ["bk"]
    assert launches(control) == [first]
    assert second == first
    assert SESSION_KEY_FIELD in wrapper._servers_cfg["bk"]
    assert wrapper._caller == consumer._executor.acall
