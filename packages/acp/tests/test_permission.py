"""Confirm гварда в ACP: session/request_permission и ответ клиента."""

import asyncio
import json

import pytest
from gc_acp.ask_human import empty_ask_human
from gc_core.security.confirm_port import request_confirm
from gc_core.security.permission_store import PermissionStore
from test_facade import (
    _append_call,
    _by_id,
    _drain,
    _init,
    _line,
    _new,
    _open,
    _updates,
)


def _permission_calls(writer):
    return [
        item
        for item in writer.sent
        if item.get("method") == "session/request_permission"
    ]


async def _wait_permission(writer):
    for _ in range(200):
        found = _permission_calls(writer)
        if found:
            return found[-1]
        await asyncio.sleep(0.01)
    raise AssertionError(writer.sent)


def _selected(option_id, *, kind="allow_once"):
    return {
        "outcome": {
            "outcome": "selected",
            "optionId": option_id,
            "kind": kind,
        }
    }


def _response(request_id, result=None, error=None):
    body = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = {} if result is None else result
    return json.dumps(body)


async def _ready(tmp_path, **settings_extra):
    facade, writer, agents = _open(
        tmp_path, settings_extra=settings_extra or None
    )
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    agent = agents[0]
    store = PermissionStore()
    agent.executor._ctx.permission_store = store
    return facade, writer, agent, session_id, store


async def _ask(
    ctx,
    decision_key,
    *,
    title="Проверка команды",
    question="Разрешить запуск?",
):
    fields = [("Команда", "ls")]

    def run():
        return request_confirm(
            title=title,
            question=question,
            fields=fields,
            ctx=ctx,
            decision_key=decision_key,
        )

    return await asyncio.to_thread(run)


async def test_confirm_request_shape_and_allow_once_reaches_store(tmp_path):
    facade, writer, agent, _session_id, store = await _ready(tmp_path)
    ctx = agent.executor._ctx

    async def reply():
        msg = await _wait_permission(writer)
        await facade.handle_line(_response(msg["id"], _selected("allow_once")))
        return msg

    task = asyncio.create_task(reply())
    allowed = await _ask(ctx, "command:ls")
    msg = await task

    assert allowed is True
    assert store.decision_for("command:ls") is True
    assert "command:ls" not in store.session_rules()
    assert ctx.ask_human is empty_ask_human
    assert msg["method"] == "session/request_permission"
    params = msg["params"]
    assert params["sessionId"]
    tool = params["toolCall"]
    assert tool["toolCallId"] == "confirm:command:ls"
    assert tool["title"] == "Проверка команды"
    assert tool["kind"] == "execute"
    assert tool["status"] == "pending"
    assert tool["content"] == [
        {
            "type": "content",
            "content": {
                "type": "text",
                "text": "Разрешить запуск?\nКоманда: ls",
            },
        }
    ]
    assert params["options"] == [
        {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
        {
            "optionId": "allow_always",
            "name": "Allow always",
            "kind": "allow_always",
        },
        {
            "optionId": "reject_once",
            "name": "Reject once",
            "kind": "reject_once",
        },
        {
            "optionId": "reject_always",
            "name": "Reject always",
            "kind": "reject_always",
        },
    ]
    assert not any(
        item.get("id") == msg["id"] and "error" in item for item in writer.sent
    )
    await facade.shutdown()


@pytest.mark.parametrize(
    ("option_id", "distorted_kind", "allowed", "remembered"),
    [
        ("allow_always", "reject_once", True, True),
        ("reject_once", "allow_always", False, False),
        ("reject_always", "allow_once", False, True),
    ],
)
async def test_option_id_wins_over_distorted_kind(
    tmp_path, option_id, distorted_kind, allowed, remembered
):
    facade, writer, agent, _session_id, store = await _ready(tmp_path)
    key = f"command:{option_id}"

    async def reply():
        msg = await _wait_permission(writer)
        await facade.handle_line(
            _response(msg["id"], _selected(option_id, kind=distorted_kind))
        )

    task = asyncio.create_task(reply())
    result = await _ask(agent.executor._ctx, key)
    await task
    assert result is allowed
    assert (key in store.session_rules()) is remembered
    if remembered:
        assert store.session_rules()[key] is allowed
    await facade.shutdown()


@pytest.mark.parametrize(
    "result",
    [
        {"outcome": {"outcome": "cancelled"}},
        {},
        {"outcome": {"outcome": "selected", "optionId": "yes"}},
        {"outcome": "selected"},
    ],
)
async def test_fail_closed_outcomes_deny_once(tmp_path, result):
    facade, writer, agent, _session_id, store = await _ready(tmp_path)
    key = "read:/tmp/a"

    async def reply():
        msg = await _wait_permission(writer)
        assert msg["params"]["toolCall"]["kind"] == "read"
        await facade.handle_line(_response(msg["id"], result))

    task = asyncio.create_task(reply())
    assert await _ask(agent.executor._ctx, key) is False
    await task
    assert key not in store.session_rules()
    await facade.shutdown()


async def test_write_guard_kind_is_edit(tmp_path):
    facade, writer, agent, _session_id, _store = await _ready(tmp_path)

    async def reply():
        msg = await _wait_permission(writer)
        assert msg["params"]["toolCall"]["kind"] == "edit"
        await facade.handle_line(
            _response(msg["id"], _selected("reject_once"))
        )

    task = asyncio.create_task(reply())
    assert await _ask(agent.executor._ctx, "write:/tmp/a") is False
    await task
    await facade.shutdown()


async def test_other_guard_kind(tmp_path):
    facade, writer, agent, _session_id, _store = await _ready(tmp_path)

    async def reply():
        msg = await _wait_permission(writer)
        assert msg["params"]["toolCall"]["kind"] == "other"
        await facade.handle_line(
            _response(msg["id"], _selected("reject_once"))
        )

    task = asyncio.create_task(reply())
    assert await _ask(agent.executor._ctx, "url:https://example.test") is False
    await task
    await facade.shutdown()


async def test_jsonrpc_error_denies_and_session_stays_up(tmp_path):
    facade, writer, agent, _session_id, store = await _ready(tmp_path)
    key = "command:rm"

    async def reply():
        msg = await _wait_permission(writer)
        await facade.handle_line(
            _response(
                msg["id"],
                error={"code": -32601, "message": "Method not found"},
            )
        )

    task = asyncio.create_task(reply())
    assert await _ask(agent.executor._ctx, key) is False
    await task
    assert key not in store.session_rules()
    await facade.handle_line(_line("session/list", {}, 9))
    assert _by_id(writer, 9)["result"]["sessions"] == []
    await facade.shutdown()


async def test_timeout_denies_and_late_allow_does_not_stick(tmp_path):
    facade, writer, agent, _session_id, store = await _ready(
        tmp_path, **{"acp.confirm_timeout_sec": 0.05}
    )
    key = "command:sleep"

    async def notice():
        return await _wait_permission(writer)

    task = asyncio.create_task(notice())
    assert await _ask(agent.executor._ctx, key) is False
    msg = await task
    await facade.handle_line(_response(msg["id"], _selected("allow_always")))
    assert store.decision_for(key) is None
    assert key not in store.export_run_rules()
    assert key not in store.session_rules()
    assert [
        item
        for item in writer.sent
        if item.get("method") == "$/cancel_request"
    ] == [
        {
            "jsonrpc": "2.0",
            "method": "$/cancel_request",
            "params": {"requestId": msg["id"]},
        }
    ]
    assert not any(
        item.get("id") == msg["id"] and "error" in item for item in writer.sent
    )

    async def allow_again():
        for _ in range(200):
            found = _permission_calls(writer)
            if len(found) >= 2:
                await facade.handle_line(
                    _response(found[-1]["id"], _selected("allow_once"))
                )
                return
            await asyncio.sleep(0.01)
        raise AssertionError(writer.sent)

    again = asyncio.create_task(allow_again())
    assert await _ask(agent.executor._ctx, key) is True
    await again
    assert len(_permission_calls(writer)) == 2
    await facade.handle_line(_line("session/list", {}, 9))
    assert _by_id(writer, 9)["result"]["sessions"] == []
    await facade.shutdown()


async def test_blocked_writer_timeout_does_not_cache_deny(tmp_path):
    """Lock писателя дольше timeout+1 не пишет run-правило и не открывает диалог."""
    facade, writer, agent, _session_id, store = await _ready(
        tmp_path, **{"acp.confirm_timeout_sec": 0.05}
    )
    key = "command:ls"
    lock = asyncio.Lock()
    writer._lock = lock

    def write_locked(payload):
        writer.sent.append(payload)

    writer.write_locked = write_locked
    await lock.acquire()
    try:
        assert await _ask(agent.executor._ctx, key) is False
    finally:
        if lock.locked():
            lock.release()
    await asyncio.sleep(0.05)
    assert _permission_calls(writer) == []
    assert store.decision_for(key) is None
    assert key not in store.export_run_rules()

    async def allow():
        msg = await _wait_permission(writer)
        await facade.handle_line(_response(msg["id"], _selected("allow_once")))

    again = asyncio.create_task(allow())
    assert await _ask(agent.executor._ctx, key) is True
    await again
    assert len(_permission_calls(writer)) == 1
    await facade.shutdown()


async def test_allow_arriving_with_timeout_is_applied(tmp_path, monkeypatch):
    """Allow в тот же тик, что TimeoutError, не подменяется отказом."""
    facade, writer, agent, _session_id, store = await _ready(tmp_path)
    key = "command:ls"

    async def racing(aw, **_kwargs):
        await aw
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", racing)

    async def reply():
        msg = await _wait_permission(writer)
        await facade.handle_line(
            _response(msg["id"], _selected("allow_always"))
        )

    task = asyncio.create_task(reply())
    assert await _ask(agent.executor._ctx, key) is True
    await task
    assert store.decision_for(key) is True
    assert store.session_rules()[key] is True
    assert not any(
        item.get("method") == "$/cancel_request" for item in writer.sent
    )
    await facade.shutdown()


async def test_foreign_response_does_not_break_dispatch(tmp_path):
    facade, writer, agent, _session_id, store = await _ready(tmp_path)
    key = "command:ls"

    async def reply():
        msg = await _wait_permission(writer)
        await facade.handle_line(
            _response("foreign", _selected("reject_always"))
        )
        await facade.handle_line(_response(msg["id"], _selected("allow_once")))
        return msg

    task = asyncio.create_task(reply())
    assert await _ask(agent.executor._ctx, key) is True
    msg = await task
    assert not any(item.get("id") == "foreign" for item in writer.sent)
    assert not any(
        item.get("id") == msg["id"] and "error" in item for item in writer.sent
    )
    assert store.decision_for(key) is True
    await facade.handle_line(_line("session/list", {}, 9))
    assert "result" in _by_id(writer, 9)
    await facade.shutdown()


@pytest.mark.parametrize("method", ["session/close", "session/delete"])
async def test_close_or_delete_during_permission_denies_and_acloses(
    tmp_path, method
):
    facade, writer, agent, session_id, store = await _ready(tmp_path)
    agent.hold = True
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "a"}],
            },
            3,
        )
    )
    await agent.started.wait()
    key = "command:ls"

    async def drop():
        msg = await _wait_permission(writer)
        await facade.handle_line(_line(method, {"sessionId": session_id}, 4))
        return msg

    task = asyncio.create_task(drop())
    assert await _ask(agent.executor._ctx, key) is False
    msg = await task
    assert _by_id(writer, 4)["result"] == {}
    assert {
        "jsonrpc": "2.0",
        "method": "$/cancel_request",
        "params": {"requestId": msg["id"]},
    } in writer.sent
    assert key not in store.session_rules()
    await _drain(facade)
    assert agent.closed is True
    await facade.shutdown()


async def test_cancel_while_send_waits_for_the_lock_skips_permission(tmp_path):
    """Отмена, пришедшая пока send ждёт lock, не открывает диалог."""
    facade, writer, agent, session_id, _store = await _ready(tmp_path)
    lock = asyncio.Lock()
    writer._lock = lock

    def write_locked(payload):
        writer.sent.append(payload)

    writer.write_locked = write_locked
    await lock.acquire()
    try:
        task = asyncio.create_task(_ask(agent.executor._ctx, "command:ls"))
        for _ in range(100):
            if facade._pending._items:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("confirm did not arm")
        await facade.handle_line(
            _line("session/cancel", {"sessionId": session_id}, notify=True)
        )
    finally:
        if lock.locked():
            lock.release()
    assert await task is False
    assert _permission_calls(writer) == []
    await facade.shutdown()


async def test_session_cancel_unblocks_confirm(tmp_path):
    facade, writer, agent, session_id, store = await _ready(tmp_path)
    key = "read:/etc/passwd"

    async def cancel():
        msg = await _wait_permission(writer)
        await facade.handle_line(
            _line("session/cancel", {"sessionId": session_id}, notify=True)
        )
        return msg

    task = asyncio.create_task(cancel())
    assert await _ask(agent.executor._ctx, key) is False
    msg = await task
    assert key not in store.session_rules()
    assert {
        "jsonrpc": "2.0",
        "method": "$/cancel_request",
        "params": {"requestId": msg["id"]},
    } in writer.sent
    await facade.shutdown()


async def test_cancel_request_on_permission_id_unblocks(tmp_path):
    facade, writer, agent, _session_id, store = await _ready(tmp_path)
    key = "command:ls"

    async def cancel():
        msg = await _wait_permission(writer)
        await facade.handle_line(
            _line(
                "$/cancel_request",
                {"requestId": msg["id"]},
                notify=True,
            )
        )

    task = asyncio.create_task(cancel())
    assert await _ask(agent.executor._ctx, key) is False
    await task
    assert key not in store.session_rules()
    await facade.shutdown()


async def test_cancel_request_on_prompt_id_unblocks_confirm(tmp_path):
    """$/cancel_request с requestId промпта снимает confirm и закрывает пикер."""
    facade, writer, agent, session_id, _store = await _ready(tmp_path)
    agent.hold = True
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "a"}],
            },
            3,
        )
    )
    await agent.started.wait()

    async def cancel():
        msg = await _wait_permission(writer)
        await facade.handle_line(
            _line("$/cancel_request", {"requestId": 3}, notify=True)
        )
        return msg

    task = asyncio.create_task(cancel())
    assert await _ask(agent.executor._ctx, "command:ls") is False
    msg = await task
    assert {
        "jsonrpc": "2.0",
        "method": "$/cancel_request",
        "params": {"requestId": msg["id"]},
    } in writer.sent
    agent.release.set()
    await facade._sessions[session_id].prompt_task
    await facade.shutdown()


async def test_child_guard_asks_same_session_before_recording(tmp_path):
    """Гвард ребёнка спрашивает ту же сессию и не пишет run-правило заранее."""
    from types import SimpleNamespace

    from gc_core.tools.subagents.runner import SubagentRunner

    facade, writer, agent, session_id, store = await _ready(tmp_path)
    parent = agent.executor._ctx
    child = SubagentRunner(parent, None)._bind_child_context(
        SimpleNamespace(), "subagent_worker"
    )
    key = "read:/etc/hosts"

    async def reply():
        msg = await _wait_permission(writer)
        assert msg["params"]["sessionId"] == session_id
        assert store.decision_for(key) is None
        assert key not in store.export_run_rules()
        await facade.handle_line(_response(msg["id"], _selected("allow_once")))

    task = asyncio.create_task(reply())
    assert await _ask(child, key) is True
    await task
    assert store.decision_for(key) is True
    assert key not in store.session_rules()
    await facade.shutdown()


async def test_shutdown_unblocks_confirm(tmp_path):
    facade, writer, agent, _session_id, _store = await _ready(tmp_path)

    async def stop():
        await _wait_permission(writer)
        await facade.shutdown()

    task = asyncio.create_task(stop())
    assert await _ask(agent.executor._ctx, "command:ls") is False
    await task


async def test_open_tool_card_id_is_reused(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    path = tmp_path / f"data_acp_{session_id}.jsonl"
    agent = agents[0]
    store = PermissionStore()
    agent.executor._ctx.permission_store = store
    agent.hold = True
    agent.on_pause = lambda: _append_call(
        path, "fs_read_file", "call-1", {"path": "src/a.py"}
    )
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "a"}],
            },
            3,
        )
    )
    for _ in range(50):
        ids = [
            item.get("toolCallId")
            for item in _updates(writer)
            if item.get("sessionUpdate") == "tool_call"
        ]
        if "call-1" in ids:
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError(writer.sent)

    async def reply():
        msg = await _wait_permission(writer)
        assert msg["params"]["toolCall"]["toolCallId"] == "call-1"
        assert msg["params"]["toolCall"]["kind"] == "read"
        await facade.handle_line(_response(msg["id"], _selected("allow_once")))

    task = asyncio.create_task(reply())
    assert await _ask(agent.executor._ctx, "read:src/a.py") is True
    await task
    agent.release.set()
    await facade._sessions[session_id].prompt_task
    await facade.shutdown()


async def test_confirm_before_poll_uses_the_call_just_written(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    path = tmp_path / f"data_acp_{session_id}.jsonl"
    agent = agents[0]
    agent.executor._ctx.permission_store = PermissionStore()
    agent.hold = True
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "a"}],
            },
            3,
        )
    )
    await agent.started.wait()
    _append_call(path, "fs_read_file", "call-early", {"path": "src/a.py"})

    async def reply():
        msg = await _wait_permission(writer)
        assert msg["params"]["toolCall"]["toolCallId"] == "call-early"
        await facade.handle_line(_response(msg["id"], _selected("allow_once")))

    task = asyncio.create_task(reply())
    assert await _ask(agent.executor._ctx, "read:src/a.py") is True
    await task
    agent.release.set()
    await facade._sessions[session_id].prompt_task
    ids = [
        item.get("toolCallId")
        for item in _updates(writer)
        if item.get("sessionUpdate") == "tool_call"
    ]
    assert ids == ["call-early"]
    await facade.shutdown()


async def test_parent_confirm_after_nested_crash_uses_parent_call(
    tmp_path, monkeypatch
):
    """Вложенный тул бросает после kind=call — confirm родителя не липнет к нему."""
    from types import SimpleNamespace

    from gc_core import processing_responses as pr

    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    path = tmp_path / f"data_acp_{session_id}.jsonl"
    agent = agents[0]
    agent.executor._ctx.permission_store = PermissionStore()
    agent.hold = True
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "a"}],
            },
            3,
        )
    )
    await agent.started.wait()
    _append_call(path, "subagent_explore", "parent-1", {"task": "найти"})

    async def boom(**_kwargs):
        raise RuntimeError("nested broke")

    monkeypatch.setattr(pr, "aapply_tool", boom)
    child = SimpleNamespace(
        parent_call_id="parent-1",
        threads_dir=str(tmp_path),
        system_id="acp",
        thread_id=session_id,
        run_id="child-1",
        subagent_progress_sink=None,
    )
    state = SimpleNamespace(tool_calls=0, steps=0, max_steps=1)
    with pytest.raises(RuntimeError, match="nested broke"):
        await pr._aexecute_tool_call(
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "nested-1",
                "arguments": "{}",
            },
            "exec_command",
            "{}",
            state,
            child,
            None,
        )
    sidecar = tmp_path / f"data_subagents_acp_{session_id}.jsonl"
    kinds = [
        json.loads(line)["kind"]
        for line in sidecar.read_text(encoding="utf-8").splitlines()
    ]
    assert kinds == ["call", "error"]

    async def reply():
        msg = await _wait_permission(writer)
        assert msg["params"]["toolCall"]["toolCallId"] == "parent-1"
        await facade.handle_line(_response(msg["id"], _selected("allow_once")))

    task = asyncio.create_task(reply())
    assert await _ask(agent.executor._ctx, "command:ls") is True
    await task
    agent.release.set()
    await facade._sessions[session_id].prompt_task
    await facade.shutdown()
