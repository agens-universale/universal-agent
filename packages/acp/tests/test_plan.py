"""plan / session_info / usage — пакет E. Клиент — память."""

import asyncio
import json
from types import SimpleNamespace

from gc_acp.facade import AcpFacade
from gc_acp.plan import (
    COST_EPSILON,
    entries_from_todos,
    plan_update,
    session_info_update,
    thread_record,
    todos_from_record,
    usage_account_from_record,
    usage_update,
    used_tokens_from_record,
)
from gc_acp.session_index import ACP_SYSTEM, prompt_title_line
from gc_core.persistence.schema import normalize_session_todo
from gc_testkit.develop_files import save_thread_meta
from gc_core.threads.facade import Threads
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_core.usage.account import MONEY_FIELD, UsageAccount
from gc_testkit.settings import make_settings


class MemoryWriter:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


class FakeAgent:
    def __init__(self):
        self.state = SimpleNamespace(
            session_todo=[],
            last_main_prompt_tokens=None,
            usage=UsageAccount(),
        )
        self.executor = SimpleNamespace(
            _ctx=SimpleNamespace(),
            last_terminal_outcome=None,
            get_state=lambda: self.state,
        )
        self.last_answer = None
        self.hold = False
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.on_pause = None
        self.lead: list = []
        self.deltas = [("answer", "ok")]
        self.answer = "ok"
        self.stopped = False
        self.error = None

    async def stream(self, prompt, **kwargs):
        del prompt, kwargs
        self.started.set()
        for item in self.lead:
            yield item
        if self.on_pause is not None:
            self.on_pause()
        if self.hold:
            await self.release.wait()
        if self.stopped:
            return
        if self.error is not None:
            raise self.error
        for item in self.deltas:
            yield item
        self.last_answer = self.answer

    def stop(self):
        self.stopped = True
        self.release.set()

    async def aclose(self):
        return None


def _open(tmp_path, factory=None):
    agents = []

    async def default_factory(config, **kwargs):
        del config, kwargs
        agent = FakeAgent()
        agents.append(agent)
        return agent

    writer = MemoryWriter()
    facade = AcpFacade(
        settings=make_settings(
            {
                "paths.threads_data_path": str(tmp_path),
                "os_backend": "local",
            }
        ),
        writer=writer,
        agent_factory=factory or default_factory,
    )
    return facade, writer, agents


def _line(method, params=None, request_id=1, *, notify=False):
    body = {"jsonrpc": "2.0", "method": method}
    if not notify:
        body["id"] = request_id
    if params is not None:
        body["params"] = params
    return json.dumps(body)


def _plans(writer):
    return [
        item for item in _updates(writer) if item["sessionUpdate"] == "plan"
    ]


def _infos(writer):
    return [
        item
        for item in _updates(writer)
        if item["sessionUpdate"] == "session_info_update"
    ]


def _usages(writer):
    return [
        item
        for item in _updates(writer)
        if item["sessionUpdate"] == "usage_update"
    ]


def _money_account(amount=0.045, currency="USD"):
    account = UsageAccount()
    account.by_category["main"] = {
        "input": 1,
        "output": 0,
        "cached": 0,
        "calls": 1,
        "missing_usage_calls": 0,
        "billed_calls": 1,
        "billed_input": 1,
        "billed_output": 0,
        "billed_cached": 0,
        MONEY_FIELD: {currency: amount},
    }
    return account


def _by_id(writer, request_id):
    found = [item for item in writer.sent if item.get("id") == request_id]
    assert found, writer.sent
    return found[-1]


def _updates(writer):
    return [
        item["params"]["update"]
        for item in writer.sent
        if item.get("method") == "session/update"
    ]


def _append_record(path, payload, ts=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {"type": "message", "payload": payload}
    if ts is not None:
        envelope["ts"] = ts
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")


def _append_call(path, name, call_id, arguments):
    _append_record(
        path,
        {
            "type": "function_call",
            "name": name,
            "call_id": call_id,
            "arguments": json.dumps(arguments),
        },
    )


def _append_output(path, call_id, output):
    _append_record(
        path,
        {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(output),
        },
    )


async def _wait_for(writer, pred, *, rounds=50):
    for _ in range(rounds):
        if pred(_updates(writer)):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(writer.sent)


async def _init(facade):
    await facade.handle_line(_line("initialize", {"protocolVersion": 1}))


async def _new(facade, cwd, request_id=2):
    await facade.handle_line(
        _line(
            "session/new",
            {"cwd": cwd, "mcpServers": []},
            request_id,
        )
    )


# --- unit: mapping -----------------------------------------------------------


def test_entries_map_status_and_default_priority():
    entries = entries_from_todos(
        [
            {"content": "A", "status": "completed"},
            {"content": "B", "status": "in_progress"},
            {"content": "C", "status": "pending"},
            {"content": "D", "status": "weird"},
            {"content": "", "status": "pending"},
            "skip",
        ]
    )
    assert entries == [
        {"content": "A", "status": "completed", "priority": "medium"},
        {"content": "B", "status": "in_progress", "priority": "medium"},
        {"content": "C", "status": "pending", "priority": "medium"},
        {"content": "D", "status": "pending", "priority": "medium"},
    ]


def test_plan_update_shape_and_empty_clear():
    assert plan_update([]) == {"sessionUpdate": "plan", "entries": []}
    assert plan_update([{"content": "x", "status": "pending"}]) == {
        "sessionUpdate": "plan",
        "entries": [
            {
                "content": "x",
                "status": "pending",
                "priority": "medium",
            }
        ],
    }


def test_session_info_and_title_line():
    assert session_info_update("hello") == {
        "sessionUpdate": "session_info_update",
        "title": "hello",
    }
    assert prompt_title_line("  first\nsecond  ") == "first"
    assert prompt_title_line("   ") is None
    assert prompt_title_line("x" * 100) == "x" * 80


def test_usage_requires_used_and_size():
    assert usage_update(used=None, size=100) is None
    assert usage_update(used=10, size=None) is None
    assert usage_update(used=-1, size=100) is None
    assert usage_update(used=10, size=0) is None
    assert usage_update(used=True, size=100) is None
    assert usage_update(used=10, size=False) is None
    assert usage_update(used=10, size=-1) is None
    body = usage_update(used=10, size=100)
    assert body == {
        "sessionUpdate": "usage_update",
        "used": 10,
        "size": 100,
    }


def test_entries_from_todos_non_list_and_strips_content():
    assert entries_from_todos(None) == []
    assert entries_from_todos({"content": "x"}) == []
    assert entries_from_todos([{"content": "  x  ", "status": "pending"}]) == [
        {"content": "x", "status": "pending", "priority": "medium"}
    ]


def _record(tmp_path, session_id):
    """Запись треда так, как её видит фасад: через хранилище тредов."""
    threads = Threads(FileThreadStore(tmp_path))
    key = ThreadKey(system=ACP_SYSTEM, thread=session_id)
    return asyncio.run(thread_record(threads, key))


def test_used_tokens_and_meta_helpers_reject_garbage(tmp_path):
    session_id = "meta-gap"
    assert todos_from_record(_record(tmp_path, session_id)) == []
    assert used_tokens_from_record(_record(tmp_path, session_id)) is None
    assert usage_account_from_record(_record(tmp_path, session_id)) is None

    save_thread_meta(
        str(tmp_path),
        ACP_SYSTEM,
        session_id,
        {
            "last_main_prompt_tokens": True,
            "shared": "not-a-dict",
            "token_usage": "broken",
        },
    )
    assert todos_from_record(_record(tmp_path, session_id)) == []
    assert used_tokens_from_record(_record(tmp_path, session_id)) is None
    # мусор вместо расхода — расхода нет
    assert usage_account_from_record(_record(tmp_path, session_id)) is None

    save_thread_meta(
        str(tmp_path),
        ACP_SYSTEM,
        session_id,
        {
            "last_main_prompt_tokens": -1,
            "shared": {},
            "token_usage": _money_account().to_meta(),
        },
    )
    assert used_tokens_from_record(_record(tmp_path, session_id)) is None
    assert todos_from_record(_record(tmp_path, session_id)) == []
    loaded = usage_account_from_record(_record(tmp_path, session_id))
    assert loaded is not None
    assert loaded.totals()[MONEY_FIELD] == {"USD": 0.045}


def test_usage_cost_only_for_single_currency():
    account = UsageAccount()
    account.by_category["main"] = {
        "input": 1,
        "output": 0,
        "cached": 0,
        "calls": 1,
        "missing_usage_calls": 0,
        "billed_calls": 1,
        "billed_input": 1,
        "billed_output": 0,
        "billed_cached": 0,
        MONEY_FIELD: {"USD": 0.045},
    }
    body = usage_update(used=1, size=100, account=account)
    assert body["cost"] == {"amount": 0.045, "currency": "USD"}

    account.by_category["main"][MONEY_FIELD] = {
        "USD": 0.01,
        "EUR": 0.02,
    }
    assert "cost" not in usage_update(used=1, size=100, account=account)

    account.by_category["main"][MONEY_FIELD] = {"": 0.01}
    assert "cost" not in usage_update(used=1, size=100, account=account)

    account.by_category["main"][MONEY_FIELD] = {"USD": COST_EPSILON / 10}
    assert "cost" not in usage_update(used=1, size=100, account=account)


# --- live: plan after session_todo -------------------------------------------


async def test_session_todo_emits_plan_next_to_tool_card(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    path = tmp_path / f"data_acp_{session_id}.jsonl"
    agent = agents[0]
    agent.lead = [("answer", "план")]
    agent.hold = True
    agent.deltas = [("answer", "готово")]
    agent.answer = "готово"
    todos = [
        {"content": "Прочитать", "status": "completed"},
        {"content": "Починить", "status": "in_progress"},
    ]

    def write_todo():
        _append_call(
            path,
            "session_todo",
            "todo-1",
            {"todos": todos},
        )
        _append_output(path, "todo-1", {"ok": True, "count": 2})
        agent.state.session_todo = list(todos)

    agent.on_pause = write_todo
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "сделай"}],
            },
            3,
        )
    )
    await _wait_for(
        writer,
        lambda u: any(item.get("sessionUpdate") == "plan" for item in u),
    )
    agent.release.set()
    await facade._sessions[session_id].prompt_task
    kinds = [item["sessionUpdate"] for item in _updates(writer)]
    assert "tool_call" in kinds
    assert "tool_call_update" in kinds
    plans = [
        item for item in _updates(writer) if item["sessionUpdate"] == "plan"
    ]
    assert len(plans) == 1
    assert plans[0]["entries"] == [
        {
            "content": "Прочитать",
            "status": "completed",
            "priority": "medium",
        },
        {
            "content": "Починить",
            "status": "in_progress",
            "priority": "medium",
        },
    ]
    # tool_call_update перед или рядом с plan, не вместо
    todo_update_i = next(
        i
        for i, item in enumerate(_updates(writer))
        if item.get("toolCallId") == "todo-1"
        and item["sessionUpdate"] == "tool_call_update"
    )
    plan_i = next(
        i
        for i, item in enumerate(_updates(writer))
        if item["sessionUpdate"] == "plan"
    )
    assert plan_i > todo_update_i
    await facade.shutdown()


async def test_session_todo_clear_sends_empty_entries(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    path = tmp_path / f"data_acp_{session_id}.jsonl"
    agent = agents[0]
    agent.hold = True

    def write_clear():
        _append_call(path, "session_todo", "clr", {"todos": []})
        _append_output(path, "clr", {"ok": True, "count": 0})
        agent.state.session_todo = []

    agent.on_pause = write_clear
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "очисти"}],
            },
            3,
        )
    )
    await _wait_for(
        writer,
        lambda u: any(item.get("sessionUpdate") == "plan" for item in u),
    )
    agent.release.set()
    await facade._sessions[session_id].prompt_task
    plans = [
        item for item in _updates(writer) if item["sessionUpdate"] == "plan"
    ]
    assert plans[-1]["entries"] == []
    await facade.shutdown()


async def test_failed_session_todo_does_not_emit_plan(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    path = tmp_path / f"data_acp_{session_id}.jsonl"
    agent = agents[0]
    agent.hold = True

    def write_fail():
        _append_call(
            path,
            "session_todo",
            "bad",
            {"todos": [{"content": "x", "status": "pending"}]},
        )
        _append_output(path, "bad", {"ok": False, "error": "boom"})

    agent.on_pause = write_fail
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "x"}],
            },
            3,
        )
    )
    await _wait_for(
        writer,
        lambda u: any(
            item.get("toolCallId") == "bad" and item.get("status") == "failed"
            for item in u
        ),
    )
    agent.release.set()
    await facade._sessions[session_id].prompt_task
    assert not any(
        item["sessionUpdate"] == "plan" for item in _updates(writer)
    )
    await facade.shutdown()


async def test_subagent_session_todo_does_not_emit_session_plan(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    path = tmp_path / f"data_acp_{session_id}.jsonl"
    sidecar = tmp_path / f"data_subagents_acp_{session_id}.jsonl"
    agent = agents[0]
    agent.hold = True

    def write_nested():
        _append_record(
            path,
            {
                "type": "function_call",
                "name": "subagent_explore",
                "call_id": "sub",
                "arguments": "{}",
            },
        )
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        with sidecar.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "ts": "1",
                        "kind": "call",
                        "parent_call_id": "sub",
                        "payload": {
                            "name": "session_todo",
                            "call_id": "nested",
                            "arguments": json.dumps(
                                {
                                    "todos": [
                                        {
                                            "content": "внутри",
                                            "status": "pending",
                                        }
                                    ]
                                }
                            ),
                        },
                    }
                )
                + "\n"
            )
            handle.write(
                json.dumps(
                    {
                        "ts": "2",
                        "kind": "result",
                        "parent_call_id": "sub",
                        "payload": {
                            "call_id": "nested",
                            "output": json.dumps({"ok": True}),
                        },
                    }
                )
                + "\n"
            )
        _append_output(path, "sub", {"ok": True})

    agent.on_pause = write_nested
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "исследуй"}],
            },
            3,
        )
    )
    await _wait_for(
        writer,
        lambda u: any(
            item.get("toolCallId") == "sub"
            and item.get("sessionUpdate") == "tool_call_update"
            for item in u
        ),
    )
    agent.release.set()
    await facade._sessions[session_id].prompt_task
    assert not any(
        item["sessionUpdate"] == "plan" for item in _updates(writer)
    )
    await facade.shutdown()


# --- title -------------------------------------------------------------------


async def test_first_prompt_sends_session_info_title_once(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "привет мир\nещё"}],
            },
            3,
        )
    )
    await facade._sessions[session_id].prompt_task
    infos = [
        item
        for item in _updates(writer)
        if item["sessionUpdate"] == "session_info_update"
    ]
    assert infos == [
        {"sessionUpdate": "session_info_update", "title": "привет мир"}
    ]
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "второй"}],
            },
            4,
        )
    )
    await facade._sessions[session_id].prompt_task
    infos = [
        item
        for item in _updates(writer)
        if item["sessionUpdate"] == "session_info_update"
    ]
    assert len(infos) == 1
    # Совпадение с session/list: та же обрезка первой строки.
    assert prompt_title_line("привет мир\nещё") == infos[0]["title"]
    await facade.shutdown()


# --- usage at end of turn ----------------------------------------------------


async def test_usage_at_turn_end_when_used_and_size_known(
    tmp_path, monkeypatch
):
    import gc_acp.plan as plan_mod

    monkeypatch.setattr(
        plan_mod, "context_window_size", lambda *_a, **_k: 200000
    )
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    agents[0].state.last_main_prompt_tokens = 53000
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "ход"}],
            },
            3,
        )
    )
    await facade._sessions[session_id].prompt_task
    usages = [
        item
        for item in _updates(writer)
        if item["sessionUpdate"] == "usage_update"
    ]
    assert usages == [
        {
            "sessionUpdate": "usage_update",
            "used": 53000,
            "size": 200000,
        }
    ]
    await facade.shutdown()


async def test_usage_skipped_without_size(tmp_path, monkeypatch):
    import gc_acp.plan as plan_mod

    monkeypatch.setattr(
        plan_mod, "context_window_size", lambda *_a, **_k: None
    )
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    agents[0].state.last_main_prompt_tokens = 100
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "ход"}],
            },
            3,
        )
    )
    await facade._sessions[session_id].prompt_task
    assert not any(
        item["sessionUpdate"] == "usage_update" for item in _updates(writer)
    )
    await facade.shutdown()


# --- load snapshot -----------------------------------------------------------


async def test_load_sends_plan_title_after_replay(tmp_path, monkeypatch):
    import gc_acp.plan as plan_mod

    monkeypatch.setattr(
        plan_mod, "context_window_size", lambda *_a, **_k: 128000
    )
    facade, writer, _agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, str(tmp_path / "proj"))
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    path = tmp_path / f"data_acp_{session_id}.jsonl"
    _append_record(
        path,
        {"role": "user", "content": "заголовок задачи"},
    )
    _append_record(
        path,
        {"role": "assistant", "content": "ок"},
    )
    save_thread_meta(
        str(tmp_path),
        ACP_SYSTEM,
        session_id,
        {
            "shared": {
                "session_todo": normalize_session_todo(
                    [
                        {"content": "Шаг", "status": "pending"},
                    ]
                )
            },
            "last_main_prompt_tokens": 42,
        },
    )
    writer.sent.clear()
    await facade.handle_line(
        _line(
            "session/load",
            {
                "sessionId": session_id,
                "cwd": str(tmp_path / "proj"),
                "mcpServers": [],
            },
            9,
        )
    )
    kinds = [item["sessionUpdate"] for item in _updates(writer)]
    assert "user_message_chunk" in kinds
    assert "agent_message_chunk" in kinds
    # после replay — строго plan → title → usage
    assert kinds[-3:] == [
        "plan",
        "session_info_update",
        "usage_update",
    ]
    plan = next(
        item for item in _updates(writer) if item["sessionUpdate"] == "plan"
    )
    assert plan["entries"] == [
        {"content": "Шаг", "status": "pending", "priority": "medium"}
    ]
    info = next(
        item
        for item in _updates(writer)
        if item["sessionUpdate"] == "session_info_update"
    )
    assert info["title"] == "заголовок задачи"
    usage = next(
        item
        for item in _updates(writer)
        if item["sessionUpdate"] == "usage_update"
    )
    assert usage["used"] == 42
    assert usage["size"] == 128000
    await facade.shutdown()


async def test_load_empty_todo_still_sends_empty_plan(tmp_path):
    facade, writer, _agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, str(tmp_path / "proj"))
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    path = tmp_path / f"data_acp_{session_id}.jsonl"
    _append_record(path, {"role": "user", "content": "hi"})
    writer.sent.clear()
    await facade.handle_line(
        _line(
            "session/load",
            {
                "sessionId": session_id,
                "cwd": str(tmp_path / "proj"),
                "mcpServers": [],
            },
            9,
        )
    )
    plans = [
        item for item in _updates(writer) if item["sessionUpdate"] == "plan"
    ]
    assert plans == [{"sessionUpdate": "plan", "entries": []}]
    await facade.shutdown()


async def test_resume_sends_plan_without_history_replay(tmp_path):
    facade, writer, _agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, str(tmp_path / "proj"))
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    path = tmp_path / f"data_acp_{session_id}.jsonl"
    _append_record(path, {"role": "user", "content": "старое"})
    save_thread_meta(
        str(tmp_path),
        ACP_SYSTEM,
        session_id,
        {
            "shared": {
                "session_todo": [
                    {"content": "R", "status": "completed"},
                ]
            }
        },
    )
    writer.sent.clear()
    await facade.handle_line(
        _line(
            "session/resume",
            {
                "sessionId": session_id,
                "cwd": str(tmp_path / "proj"),
            },
            8,
        )
    )
    kinds = [item["sessionUpdate"] for item in _updates(writer)]
    assert "user_message_chunk" not in kinds
    assert {"plan", "session_info_update"} <= set(kinds)
    plan = next(
        item for item in _updates(writer) if item["sessionUpdate"] == "plan"
    )
    assert plan["entries"][0]["content"] == "R"
    await facade.shutdown()


# --- gaps: stub usage / title after snapshot / cancel·fail -----------------


async def test_usage_silent_when_context_window_size_is_stub(tmp_path):
    """Без probe size всегда None — usage не шлём, даже при известных tokens."""
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    agents[0].state.last_main_prompt_tokens = 53000
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "ход"}],
            },
            3,
        )
    )
    await facade._sessions[session_id].prompt_task
    assert _usages(writer) == []

    path = tmp_path / f"data_acp_{session_id}.jsonl"
    _append_record(path, {"role": "user", "content": "meta"})
    save_thread_meta(
        str(tmp_path),
        ACP_SYSTEM,
        session_id,
        {"last_main_prompt_tokens": 99},
    )
    writer.sent.clear()
    await facade.handle_line(
        _line(
            "session/load",
            {
                "sessionId": session_id,
                "cwd": "/work/app",
                "mcpServers": [],
            },
            9,
        )
    )
    assert _usages(writer) == []
    await facade.shutdown()


async def test_title_not_resent_after_load_or_resume(tmp_path):
    facade, writer, _agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, str(tmp_path / "proj"))
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    path = tmp_path / f"data_acp_{session_id}.jsonl"
    _append_record(path, {"role": "user", "content": "из load"})
    writer.sent.clear()
    await facade.handle_line(
        _line(
            "session/load",
            {
                "sessionId": session_id,
                "cwd": str(tmp_path / "proj"),
                "mcpServers": [],
            },
            9,
        )
    )
    assert _infos(writer) == [
        {"sessionUpdate": "session_info_update", "title": "из load"}
    ]
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "другой текст"}],
            },
            10,
        )
    )
    await facade._sessions[session_id].prompt_task
    assert len(_infos(writer)) == 1

    # resume тоже ставит title_sent — повторный prompt не дублирует
    await facade.handle_line(
        _line("session/close", {"sessionId": session_id}, 11)
    )
    writer.sent.clear()
    await facade.handle_line(
        _line(
            "session/resume",
            {
                "sessionId": session_id,
                "cwd": str(tmp_path / "proj"),
            },
            12,
        )
    )
    assert _infos(writer) == [
        {"sessionUpdate": "session_info_update", "title": "из load"}
    ]
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "после resume"}],
            },
            13,
        )
    )
    await facade._sessions[session_id].prompt_task
    assert len(_infos(writer)) == 1
    await facade.shutdown()


async def test_cancelled_or_failed_turn_skips_usage(tmp_path, monkeypatch):
    import gc_acp.plan as plan_mod

    monkeypatch.setattr(
        plan_mod, "context_window_size", lambda *_a, **_k: 200000
    )
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]

    agents[0].hold = True
    agents[0].state.last_main_prompt_tokens = 1000
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "cancel me"}],
            },
            3,
        )
    )
    await agents[0].started.wait()
    await facade.handle_line(
        _line("session/cancel", {"sessionId": session_id}, notify=True)
    )
    await facade._sessions[session_id].prompt_task
    assert _by_id(writer, 3)["result"] == {"stopReason": "cancelled"}
    assert _usages(writer) == []

    writer.sent.clear()
    agents[0].hold = False
    agents[0].stopped = False
    agents[0].error = RuntimeError("boom")
    agents[0].state.last_main_prompt_tokens = 2000
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "fail me"}],
            },
            4,
        )
    )
    await facade._sessions[session_id].prompt_task
    assert _by_id(writer, 4)["error"]["code"] == -32603
    assert _usages(writer) == []
    await facade.shutdown()


async def test_usage_includes_cost_from_agent_and_meta(tmp_path, monkeypatch):
    import gc_acp.plan as plan_mod

    monkeypatch.setattr(
        plan_mod, "context_window_size", lambda *_a, **_k: 128000
    )
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, str(tmp_path / "proj"))
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    agents[0].state.last_main_prompt_tokens = 11
    agents[0].state.usage = _money_account(0.12, "USD")
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "с cost"}],
            },
            3,
        )
    )
    await facade._sessions[session_id].prompt_task
    assert _usages(writer) == [
        {
            "sessionUpdate": "usage_update",
            "used": 11,
            "size": 128000,
            "cost": {"amount": 0.12, "currency": "USD"},
        }
    ]

    path = tmp_path / f"data_acp_{session_id}.jsonl"
    _append_record(path, {"role": "user", "content": "load cost"})
    save_thread_meta(
        str(tmp_path),
        ACP_SYSTEM,
        session_id,
        {
            "last_main_prompt_tokens": 7,
            "token_usage": _money_account(0.03, "EUR").to_meta(),
        },
    )
    writer.sent.clear()
    await facade.handle_line(
        _line(
            "session/load",
            {
                "sessionId": session_id,
                "cwd": str(tmp_path / "proj"),
                "mcpServers": [],
            },
            9,
        )
    )
    usage = next(item for item in _usages(writer))
    assert usage["used"] == 7
    assert usage["size"] == 128000
    assert usage["cost"] == {"amount": 0.03, "currency": "EUR"}
    await facade.shutdown()


# --- gaps: plan edge cases -------------------------------------------------


async def test_plan_empty_when_agent_state_unavailable(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    path = tmp_path / f"data_acp_{session_id}.jsonl"
    agent = agents[0]
    agent.hold = True
    todos = [{"content": "Шаг", "status": "pending"}]

    def write_todo_then_break_state():
        _append_call(path, "session_todo", "todo-1", {"todos": todos})
        _append_output(path, "todo-1", {"ok": True, "count": 1})
        agent.state.session_todo = list(todos)

        def boom():
            raise RuntimeError("state gone")

        agent.executor.get_state = boom

    agent.on_pause = write_todo_then_break_state
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "todo"}],
            },
            3,
        )
    )
    await _wait_for(
        writer,
        lambda u: any(item.get("sessionUpdate") == "plan" for item in u),
    )
    agent.release.set()
    await facade._sessions[session_id].prompt_task
    assert _plans(writer) == [{"sessionUpdate": "plan", "entries": []}]
    await facade.shutdown()


async def test_two_session_todo_calls_emit_two_plans(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    path = tmp_path / f"data_acp_{session_id}.jsonl"
    agent = agents[0]
    agent.hold = True
    first = [{"content": "A", "status": "pending"}]
    second = [
        {"content": "A", "status": "completed"},
        {"content": "B", "status": "in_progress"},
    ]

    def write_first():
        _append_call(path, "session_todo", "todo-1", {"todos": first})
        _append_output(path, "todo-1", {"ok": True, "count": 1})
        agent.state.session_todo = list(first)

    agent.on_pause = write_first
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "два todo"}],
            },
            3,
        )
    )
    await _wait_for(
        writer,
        lambda u: len([x for x in u if x.get("sessionUpdate") == "plan"]) == 1,
    )
    _append_call(path, "session_todo", "todo-2", {"todos": second})
    _append_output(path, "todo-2", {"ok": True, "count": 2})
    agent.state.session_todo = list(second)
    await _wait_for(
        writer,
        lambda u: len([x for x in u if x.get("sessionUpdate") == "plan"]) == 2,
    )
    agent.release.set()
    await facade._sessions[session_id].prompt_task
    plans = _plans(writer)
    assert len(plans) == 2
    assert plans[0]["entries"] == [
        {"content": "A", "status": "pending", "priority": "medium"}
    ]
    assert plans[1]["entries"] == [
        {"content": "A", "status": "completed", "priority": "medium"},
        {"content": "B", "status": "in_progress", "priority": "medium"},
    ]
    await facade.shutdown()


async def test_session_todo_in_progress_does_not_emit_plan(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    path = tmp_path / f"data_acp_{session_id}.jsonl"
    agent = agents[0]
    agent.hold = True
    todos = [{"content": "Ждём", "status": "pending"}]

    def write_call_only():
        _append_call(path, "session_todo", "todo-1", {"todos": todos})

    agent.on_pause = write_call_only
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "жди"}],
            },
            3,
        )
    )
    await _wait_for(
        writer,
        lambda u: any(
            item.get("toolCallId") == "todo-1"
            and item.get("sessionUpdate") == "tool_call"
            for item in u
        ),
    )
    assert _plans(writer) == []
    _append_output(path, "todo-1", {"ok": True, "count": 1})
    agent.state.session_todo = list(todos)
    await _wait_for(
        writer,
        lambda u: any(item.get("sessionUpdate") == "plan" for item in u),
    )
    agent.release.set()
    await facade._sessions[session_id].prompt_task
    assert len(_plans(writer)) == 1
    await facade.shutdown()


async def test_resume_sends_usage_when_size_known(tmp_path, monkeypatch):
    import gc_acp.plan as plan_mod

    monkeypatch.setattr(
        plan_mod, "context_window_size", lambda *_a, **_k: 64000
    )
    facade, writer, _agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, str(tmp_path / "proj"))
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    path = tmp_path / f"data_acp_{session_id}.jsonl"
    _append_record(path, {"role": "user", "content": "resume title"})
    save_thread_meta(
        str(tmp_path),
        ACP_SYSTEM,
        session_id,
        {
            "shared": {
                "session_todo": [
                    {"content": "R", "status": "completed"},
                ]
            },
            "last_main_prompt_tokens": 5,
        },
    )
    writer.sent.clear()
    await facade.handle_line(
        _line(
            "session/resume",
            {
                "sessionId": session_id,
                "cwd": str(tmp_path / "proj"),
            },
            8,
        )
    )
    kinds = [item["sessionUpdate"] for item in _updates(writer)]
    assert kinds == [
        "plan",
        "session_info_update",
        "usage_update",
    ]
    assert _usages(writer)[0]["used"] == 5
    assert _usages(writer)[0]["size"] == 64000
    await facade.shutdown()


async def test_load_skips_session_info_when_only_technical_user(tmp_path):
    facade, writer, _agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, str(tmp_path / "proj"))
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    path = tmp_path / f"data_acp_{session_id}.jsonl"
    _append_record(
        path,
        {
            "role": "user",
            "content": '<technical type="technical">служебное</technical>',
        },
    )
    writer.sent.clear()
    await facade.handle_line(
        _line(
            "session/load",
            {
                "sessionId": session_id,
                "cwd": str(tmp_path / "proj"),
                "mcpServers": [],
            },
            9,
        )
    )
    assert _plans(writer) == [{"sessionUpdate": "plan", "entries": []}]
    assert _infos(writer) == []
    await facade.shutdown()
