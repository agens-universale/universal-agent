"""Контекст текущего вызова тула: кто сейчас исполняется."""

import asyncio
import json

from gc_core.execution.tool_call_context import (
    bind_tool_call,
    current_tool_call,
)
from gc_core.execution.tool_effects import execute_native_tool_effect
from gc_core.tools import aapply_tool
from gc_core.tools.exec.execute_command import ExecuteCommand
from gc_core.tools.fs.write_to_file_tool import WriteToFile
from gc_core.user_stop import UserStop, bind_user_stop
from gc_testkit.helpers.tool_ctx import local_ctx


def test_unbound_is_empty():
    assert current_tool_call() == ("", "")


def test_bound_inside_and_reset_after():
    with bind_tool_call("exec_command", "c1"):
        assert current_tool_call() == ("exec_command", "c1")
    assert current_tool_call() == ("", "")


def test_nested_binding_restores_outer():
    with bind_tool_call("exec_command", "outer"):
        with bind_tool_call("exec_python", "inner"):
            assert current_tool_call() == ("exec_python", "inner")
        assert current_tool_call() == ("exec_command", "outer")


def test_none_values_become_empty_strings():
    with bind_tool_call(None, None):
        assert current_tool_call() == ("", "")


async def test_value_is_visible_in_to_thread():
    with bind_tool_call("exec_command", "c1"):
        seen = await asyncio.to_thread(current_tool_call)
    assert seen == ("exec_command", "c1")


def _registry():
    name = WriteToFile.registered_tool_name()
    return name, {
        name: {
            "tool_fabric": WriteToFile,
            "spec": WriteToFile.tool_description(),
        }
    }


async def test_aapply_tool_binds_call_in_acall_branch(tmp_path, monkeypatch):
    name, registry = _registry()
    ctx = local_ctx({"launch_root": str(tmp_path)})
    ctx.tools_data = registry
    seen = []
    original = WriteToFile.acall

    async def spy(self, **kwargs):
        seen.append(current_tool_call())
        return await original(self, **kwargs)

    monkeypatch.setattr(WriteToFile, "acall", spy)
    output = await aapply_tool(
        name,
        {"path": str(tmp_path / "a.txt"), "content": "x"},
        ctx=ctx,
        call_id="call-a",
    )
    assert json.loads(output)["ok"] is True
    assert seen == [(name, "call-a")]
    assert current_tool_call() == ("", "")


async def test_aapply_tool_binds_call_in_effect_port_branch(tmp_path):
    name, registry = _registry()
    config = {"launch_root": str(tmp_path)}
    core, worker = local_ctx(config), local_ctx(config)
    core.tools_data = worker.tools_data = registry
    seen = []

    async def effect(request):
        seen.append(current_tool_call())
        return await execute_native_tool_effect(request, ctx=worker)

    core.tool_effect = effect
    output = await aapply_tool(
        name,
        {"path": str(tmp_path / "b.txt"), "content": "y"},
        ctx=core,
        call_id="call-b",
    )
    assert json.loads(output)["ok"] is True
    assert seen == [(name, "call-b")]
    assert current_tool_call() == ("", "")


class _StopLog:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def info(self, msg, *args, extra=None) -> None:
        del msg, args
        self.records.append(dict(extra or {}))

    def events(self, name: str) -> list[dict]:
        return [r for r in self.records if r.get("event") == name]


def _exec_registry():
    name = ExecuteCommand.registered_tool_name()
    return name, {
        name: {
            "tool_fabric": ExecuteCommand,
            "spec": ExecuteCommand.tool_description(),
        }
    }


async def test_stop_event_of_command_carries_bound_call(tmp_path):
    """Снятие команды пишет её тул и ``call_id``: ``UserStop.arm`` читает
    ту же переменную, что ставит ``aapply_tool``."""
    name, registry = _exec_registry()
    ctx = local_ctx({"launch_root": str(tmp_path)})
    ctx.tools_data = registry
    log = _StopLog()
    gate = UserStop()
    gate.prepare_run(asyncio.get_running_loop(), log)

    async def _work():
        with bind_user_stop(gate):
            return await aapply_tool(
                name, {"command": "sleep 30"}, ctx=ctx, call_id="call-s"
            )

    task = asyncio.create_task(_work())
    await asyncio.sleep(0.5)
    gate.request(log)
    await asyncio.wait_for(task, 10)
    (killed,) = log.events("user_stop_killed")
    assert (killed["kind"], killed["tool"], killed["call_id"]) == (
        "exec",
        name,
        "call-s",
    )
