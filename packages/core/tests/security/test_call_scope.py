"""Привязка к вызову инструмента: что принадлежит одному вызову и не утекает
в соседний — в том числе когда вызовы идут одновременно."""

import asyncio

import pytest

from gc_core.security.call_scope import call_scope, current_call
from gc_core.security.permission_store import PermissionStore

pytestmark = pytest.mark.asyncio


async def test_no_scope_outside_a_tool_call():
    assert current_call() is None


async def test_scope_carries_the_real_state_and_restores_the_outer_one():
    outer_state, inner_state = object(), object()
    with call_scope(state=outer_state, call_id="c1", tool_name="exec_command"):
        outer = current_call()
        assert (outer.state, outer.call_id, outer.tool_name) == (
            outer_state,
            "c1",
            "exec_command",
        )
        # вложенный прогон субагента ставит свою привязку и возвращает нашу
        with call_scope(
            state=inner_state, call_id="c2", tool_name="fs_write_file"
        ):
            assert current_call().state is inner_state
        assert current_call() is outer
    assert current_call() is None


async def test_one_time_answer_lives_for_a_single_tool_call():
    """«Разово» — один вызов тула. Ключ записи — путь без содержимого: держи
    ответ дольше, и одно разрешение покрыло бы любые следующие правки файла."""
    store = PermissionStore()
    with call_scope(state=None, call_id="c1", tool_name="fs_write_file"):
        store.record("write:/p/a.txt", True, session=False)
        # внутри вызова ответ помнится: вторая проверка того же действия не спросит
        assert store.decision_for("write:/p/a.txt") is True
    with call_scope(state=None, call_id="c2", tool_name="fs_write_file"):
        assert store.decision_for("write:/p/a.txt") is None


async def test_session_answer_survives_tool_calls():
    store = PermissionStore()
    with call_scope(state=None, call_id="c1", tool_name="fs_write_file"):
        store.record("write:/p/a.txt", False, session=True)
    with call_scope(state=None, call_id="c2", tool_name="fs_write_file"):
        assert store.decision_for("write:/p/a.txt") is False


async def test_concurrent_calls_do_not_see_each_others_one_time_answers():
    """Стор у родителя и субагентов один. Пока вызовы шли по одному, разовый
    ответ считался по общему счётчику; при одновременных вызовах чужой вызов
    гасил бы ответ между двумя проверками одного действия."""
    store = PermissionStore()
    first_recorded = asyncio.Event()
    second_started = asyncio.Event()
    seen: dict[str, object] = {}

    async def first():
        with call_scope(state=None, call_id="a", tool_name="fs_write_file"):
            store.record("write:/p/a.txt", True, session=False)
            first_recorded.set()
            await second_started.wait()  # сосед начал свой вызов
            seen["first_after"] = store.decision_for("write:/p/a.txt")

    async def second():
        await first_recorded.wait()
        with call_scope(state=None, call_id="b", tool_name="fs_write_file"):
            seen["second"] = store.decision_for("write:/p/a.txt")
            second_started.set()

    await asyncio.gather(first(), second())
    assert seen == {"second": None, "first_after": True}


async def test_scope_object_is_shared_with_a_guard_running_in_a_thread():
    """Защита записи у скачивания файла крутится в потоке с копией контекста:
    запись из потока обязана быть видна вызову."""
    import contextvars

    store = PermissionStore()
    with call_scope(state=None, call_id="c1", tool_name="download_file"):
        context = contextvars.copy_context()
        await asyncio.to_thread(
            context.run, store.record, "write:/p/a.bin", True, session=False
        )
        assert store.decision_for("write:/p/a.bin") is True


class _SpyTool:
    """Тул с частным срезом состояния: диспетчер подменяет ему `state`."""

    owns_session_state = True
    seen: dict = {}

    def __init__(self, **kw):
        self._state = kw.get("state")

    @classmethod
    def tool_description(cls):
        return {
            "type": "function",
            "name": "t_spy",
            "parameters": {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
                "additionalProperties": False,
            },
        }

    async def acall(self, x: str) -> str:
        scope = current_call()
        type(self).seen = {
            "tool_state": self._state,
            "scope_state": scope.state,
            "call_id": scope.call_id,
            "tool_name": scope.tool_name,
            "arguments": scope.arguments,
        }
        return "ok"


async def test_dispatcher_binds_the_real_state_before_the_private_slice():
    """`fetch_url` и другие тулы с частным срезом видят в защите только
    срез без переписки — судье действий оттуда нечего показать. Привязка
    несёт настоящее состояние прогона и подготовленные аргументы."""
    from gc_testkit.settings import fake_ctx

    from collections import OrderedDict
    from types import SimpleNamespace

    from gc_core.tools import aapply_tool_result

    state = SimpleNamespace(
        shared=SimpleNamespace(
            working_memory={}, fetch_url_cache=OrderedDict()
        ),
        session_todo=[],
        messages=["переписка прогона"],
    )
    data = {
        "t_spy": {
            "tool_fabric": _SpyTool,
            "spec": _SpyTool.tool_description(),
            "description": "",
        }
    }
    await aapply_tool_result(
        "t_spy",
        '{"x": "1"}',
        data,
        fake_ctx(tools_data=data),
        state=state,
        call_id="call_7",
    )
    seen = _SpyTool.seen
    assert seen["scope_state"] is state
    assert seen["tool_state"] is not state  # тулу достался срез
    assert (seen["call_id"], seen["tool_name"]) == ("call_7", "t_spy")
    assert seen["arguments"] == {"x": "1"}
    assert current_call() is None


async def test_subagent_launch_exposes_its_real_arguments():
    """Подготовка запуска субагента отдаёт диспетчеру пустые аргументы тула;
    судье действий нужно задание, с которым субагент запускается."""
    from gc_core.tools import _SubagentTool

    tool = _SubagentTool(
        None,
        tool_name="subagent_worker",
        arguments={"task": "собери отчёт и запушь"},
        run_agent_func=None,
        parent_call_id="c1",
    )
    assert tool.launch_arguments == {"task": "собери отчёт и запушь"}


async def test_dispatcher_binds_the_launch_arguments_of_a_subagent(
    monkeypatch,
):
    """Через диспетчер: подготовка запуска субагента возвращает пустые
    аргументы тула, а в привязку должно попасть задание."""
    from gc_testkit.settings import fake_ctx

    from gc_core import tools as tools_module

    seen = {}

    class _Launch:
        launch_arguments = {"task": "собери отчёт и запушь"}

        async def acall(self) -> str:
            seen["arguments"] = current_call().arguments
            return "done"

    monkeypatch.setattr(
        tools_module,
        "_prepare_tool_call",
        lambda *a, **kw: (_Launch(), {}, []),
    )
    data = {"subagent_worker": {"category": "state"}}
    out = await tools_module.aapply_tool(
        "subagent_worker",
        "{}",
        data,
        ctx=fake_ctx(tools_data=data),
        call_id="c1",
    )
    assert out == "done"
    assert seen["arguments"] == {"task": "собери отчёт и запушь"}
