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
    assert store.decision_for(key) is not True
    assert key not in store.session_rules()
    await facade.handle_line(_line("session/list", {}, 9))
    assert _by_id(writer, 9)["result"]["sessions"] == []
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
    foreign = [item for item in writer.sent if item.get("id") == "foreign"]
    assert foreign and foreign[-1]["error"]["code"] == -32600
    assert not any(
        item.get("id") == msg["id"] and "error" in item for item in writer.sent
    )
    assert store.decision_for(key) is True
    await facade.handle_line(_line("session/list", {}, 9))
    assert "result" in _by_id(writer, 9)
    await facade.shutdown()


async def test_session_cancel_unblocks_confirm(tmp_path):
    facade, writer, agent, session_id, store = await _ready(tmp_path)
    key = "read:/etc/passwd"

    async def cancel():
        await _wait_permission(writer)
        await facade.handle_line(
            _line("session/cancel", {"sessionId": session_id}, notify=True)
        )

    task = asyncio.create_task(cancel())
    assert await _ask(agent.executor._ctx, key) is False
    await task
    assert key not in store.session_rules()
    await facade.shutdown()


async def test_cancel_request_on_permission_id_unblocks(tmp_path):
    facade, writer, agent, _session_id, store = await _ready(tmp_path)
    key = "command:ls"

    async def cancel():
        msg = await _wait_permission(writer)
        await facade.handle_line(
            _line("$/cancel_request", {"id": msg["id"]}, notify=True)
        )

    task = asyncio.create_task(cancel())
    assert await _ask(agent.executor._ctx, key) is False
    await task
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
