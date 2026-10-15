"""Цикл методов ACP на подставном агенте, без редактора и без модели."""

import asyncio
import importlib.metadata
import json
from pathlib import Path
from types import SimpleNamespace

from gc_acp.facade import AcpFacade
from gc_core.threads.facade import Threads, run_sync
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_core.threads.session_port import SessionPort
from gc_testkit.settings import make_settings


class MemoryWriter:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


class GatedWriter(MemoryWriter):
    """Задерживает result, чтобы отмена успела прийти после решения stopReason."""

    def __init__(self):
        super().__init__()
        self.gate = False
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, payload):
        if self.gate and "result" in payload:
            self.gate = False
            self.reached.set()
            await self.release.wait()
        await super().send(payload)


class FakeAgent:
    def __init__(self):
        self.executor = SimpleNamespace(
            _ctx=SimpleNamespace(),
            last_terminal_outcome=None,
        )
        self.last_answer = None
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.hold = False
        self.release_on_stop = True
        self.stopped = False
        self.stop_calls = 0
        self.closed = False
        self.deltas = [("answer", "ok")]
        self.lead: list[tuple[str, str]] = []
        self.on_pause = None
        self.answer = "ok"
        self.outcome = None
        self.error = None
        self.cancel_raises = False

    async def stream(self, prompt, **kwargs):
        self.calls.append((prompt, dict(kwargs)))
        self.started.set()
        for item in self.lead:
            yield item
        if self.on_pause is not None:
            self.on_pause()
        if self.hold:
            await self.release.wait()
        if self.stopped and self.cancel_raises:
            raise asyncio.CancelledError()
        if self.error is not None and (self.stopped or not self.hold):
            raise self.error
        if self.stopped:
            self.last_answer = None
            return
        for item in self.deltas:
            yield item
        self.last_answer = self.answer
        self.executor.last_terminal_outcome = self.outcome

    def stop(self):
        self.stop_calls += 1
        self.stopped = True
        if self.release_on_stop:
            self.release.set()

    async def aclose(self):
        self.closed = True


def _open(tmp_path, factory=None):
    agents = []

    async def default_factory(config, **kwargs):
        agent = FakeAgent()
        agent.seen_config = config
        agent.factory_kwargs = kwargs
        agents.append(agent)
        return agent

    writer = MemoryWriter()
    facade = AcpFacade(
        settings=make_settings(
            {"paths.threads_data_path": str(tmp_path), "os_backend": "local"}
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


def _by_id(writer, request_id):
    found = [item for item in writer.sent if item.get("id") == request_id]
    assert found, writer.sent
    return found[-1]


class Thread:
    """Тред сессии глазами ядра: пишет историю и трассу субагентов тем же
    путём, что исполнитель (синхронно — из колбэков подставного агента)."""

    def __init__(self, tmp_path, session_id, *, system="acp"):
        self.threads = Threads(FileThreadStore(tmp_path))
        self.key = ThreadKey(system=system, thread=session_id)

    def append(self, *messages):
        for message in messages:
            run_sync(self.threads.append_messages(self.key, [message]))

    def call(self, name, call_id, arguments):
        self.append(
            {
                "type": "function_call",
                "name": name,
                "call_id": call_id,
                "arguments": json.dumps(arguments),
            }
        )

    def output(self, call_id, output):
        self.append(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(output),
            }
        )

    def subagent(self, kind, payload):
        run_sync(
            SessionPort(self.threads.store).aappend_subagent_trace(
                self.key.system,
                self.key.thread,
                parent_call_id="sub",
                child_run_id="child",
                kind=kind,
                payload=payload,
            )
        )

    def exists(self):
        return run_sync(self.threads.get(self.key)) is not None

    def messages(self):
        return len(run_sync(self.threads.history(self.key)))


async def _wait_for_tool(writer, call_id):
    for _ in range(50):
        seen = [
            item.get("toolCallId")
            for item in _updates(writer)
            if item.get("sessionUpdate") == "tool_call_update"
        ]
        if call_id in seen:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(writer.sent)


def _updates(writer):
    return [
        item["params"]["update"]
        for item in writer.sent
        if item.get("method") == "session/update"
    ]


async def _init(facade):
    await facade.handle_line(_line("initialize", {"protocolVersion": 1}))


async def _new(facade, cwd, request_id=2, **extra):
    params = {"cwd": cwd, "mcpServers": []}
    params.update(extra)
    await facade.handle_line(_line("session/new", params, request_id))


async def test_initialize_shape_and_blocks_session_until_then(tmp_path):
    facade, writer, _agents = _open(tmp_path)
    await facade.handle_line(
        _line("session/new", {"cwd": "/work", "mcpServers": []}, 1)
    )
    assert _by_id(writer, 1)["error"]["code"] == -32600
    await _init(facade)
    result = _by_id(writer, 1)["result"]
    # initialize переиспользует id 1 — берём последнее сообщение с result.
    result = [item for item in writer.sent if item.get("id") == 1][-1][
        "result"
    ]
    assert result["protocolVersion"] == 1
    assert result["agentCapabilities"]["loadSession"] is True
    assert result["agentCapabilities"]["promptCapabilities"] == {
        "image": False,
        "audio": False,
        "embeddedContext": False,
    }
    assert result["agentCapabilities"]["mcpCapabilities"] == {
        "http": False,
        "sse": False,
    }
    assert result["agentInfo"]["name"] == "gc-acp"
    assert result["agentInfo"]["version"] == importlib.metadata.version(
        "gc-acp"
    )
    assert result["authMethods"] == []
    assert "modes" not in result
    assert "configOptions" not in result
    await facade.shutdown()


async def test_prompt_streams_and_second_turn_keeps_the_thread(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    agent = agents[0]
    agent.deltas = [("think", "hmm"), ("tool", "{"), ("answer", "hi")]
    agent.answer = "hi"
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [
                    {"type": "text", "text": "see"},
                    {
                        "type": "resource_link",
                        "name": "readme",
                        "uri": "file:///tmp/readme",
                    },
                ],
            },
            3,
        )
    )
    await agent.started.wait()
    await facade._sessions[session_id].prompt_task
    kinds = [item["sessionUpdate"] for item in _updates(writer)]
    assert kinds == ["agent_thought_chunk", "agent_message_chunk"]
    assert _by_id(writer, 3)["result"] == {"stopReason": "end_turn"}
    assert agent.calls[0][0] == "see\nreadme (file:///tmp/readme)"
    assert agent.calls[0][1]["thread_id"] == session_id
    assert agent.calls[0][1]["system"] == "acp"
    assert agent.seen_config.runtime.launch_root == "/work/app"
    assert facade._settings.runtime.launch_root is None
    assert await agent.executor._ctx.ask_human("вопрос?") == ""

    agent.deltas = [("answer", "ещё")]
    agent.answer = "ещё"
    agent.started = asyncio.Event()
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "дальше"}],
            },
            4,
        )
    )
    await facade._sessions[session_id].prompt_task
    assert len(agents) == 1
    assert agent.calls[1][1]["thread_id"] == session_id
    assert _by_id(writer, 4)["result"]["stopReason"] == "end_turn"
    await facade.shutdown()


def _finish_answer():
    return json.dumps(
        {
            "solution_type": "direct_answer",
            "task": "прочитать README",
            "task_report": "В README одна фраза.",
            "comment": "файл короткий",
            "handoff": '{"artifacts":["README.md"]}',
            "message": "Finish gate: accept.",
            "finish_gate": {"action": "accept"},
        },
        ensure_ascii=False,
    )


async def test_prompt_renders_finish_arguments_as_the_report(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    agent = agents[0]
    agent.deltas = []
    agent.answer = _finish_answer()
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
    await facade._sessions[session_id].prompt_task
    texts = [
        item["content"]["text"]
        for item in _updates(writer)
        if item["sessionUpdate"] == "agent_message_chunk"
    ]
    assert texts == ["В README одна фраза.\n\n* заметка: файл короткий"]
    assert all("Finish gate" not in text for text in texts)
    await facade.shutdown()


async def test_prompt_does_not_repeat_a_streamed_report(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    agent = agents[0]
    agent.deltas = [("answer", "В README одна фраза.")]
    agent.answer = _finish_answer()
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
    await facade._sessions[session_id].prompt_task
    texts = [
        item["content"]["text"]
        for item in _updates(writer)
        if item["sessionUpdate"] == "agent_message_chunk"
    ]
    assert texts == ["В README одна фраза."]
    await facade.shutdown()


async def test_prompt_reports_a_tool_call_between_prose(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    thread = Thread(tmp_path, session_id)
    thread.call("fs_read_file", "old", {"path": "old.py"})
    agent = agents[0]
    agent.lead = [("answer", "смотрю")]
    agent.hold = True
    agent.deltas = [("answer", "готово")]
    agent.answer = "готово"

    def write_call():
        thread.call("fs_read_file", "call-1", {"path": "src/a.py"})
        thread.call("turn_finish", "fin", {})
        thread.output("call-1", {"ok": True, "total_lines": 3})

    agent.on_pause = write_call
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
    await _wait_for_tool(writer, "call-1")
    agent.release.set()
    await facade._sessions[session_id].prompt_task
    flow = [
        (item["sessionUpdate"], item.get("toolCallId"), item.get("status"))
        for item in _updates(writer)
    ]
    assert flow == [
        ("agent_message_chunk", None, None),
        ("tool_call", "call-1", "in_progress"),
        ("tool_call_update", "call-1", "completed"),
        ("agent_message_chunk", None, None),
        ("agent_message_chunk", None, None),
    ]
    titles = [item.get("title") for item in _updates(writer)]
    assert titles[1] == "fs_read_file(path='src/a.py')"
    assert _by_id(writer, 3)["result"] == {"stopReason": "end_turn"}
    await facade.shutdown()


async def test_prompt_reports_tools_inside_a_subagent(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    thread = Thread(tmp_path, session_id)
    agent = agents[0]
    agent.lead = [("answer", "смотрю")]
    agent.hold = True
    agent.deltas = [("answer", "готово")]
    agent.answer = "готово"

    def write_call():
        thread.call("subagent_explore", "sub", {"task": "найти"})
        thread.subagent(
            "call",
            {
                "type": "function_call",
                "name": "fs_read_file",
                "call_id": "c1",
                "arguments": json.dumps({"path": "a.py"}),
            },
        )
        thread.subagent(
            "result",
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": json.dumps({"ok": True}),
            },
        )
        thread.output("sub", {"ok": True})

    agent.on_pause = write_call
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
    await _wait_for_tool(writer, "sub:c1")
    agent.release.set()
    await facade._sessions[session_id].prompt_task
    flow = [
        (item["sessionUpdate"], item.get("toolCallId"), item.get("status"))
        for item in _updates(writer)
    ]
    assert flow == [
        ("agent_message_chunk", None, None),
        ("tool_call", "sub", "in_progress"),
        ("tool_call", "sub:c1", "in_progress"),
        ("tool_call_update", "sub:c1", "completed"),
        ("tool_call_update", "sub", "completed"),
        ("agent_message_chunk", None, None),
        ("agent_message_chunk", None, None),
    ]
    await facade.shutdown()


async def test_second_parallel_prompt_is_rejected(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    agents[0].hold = True
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
    await agents[0].started.wait()
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "b"}],
            },
            4,
        )
    )
    assert _by_id(writer, 4)["error"]["code"] == -32603
    assert len(agents[0].calls) == 1
    agents[0].release.set()
    await facade._sessions[session_id].prompt_task
    assert _by_id(writer, 3)["result"]["stopReason"] == "end_turn"
    await facade.shutdown()


async def test_cancel_notification_returns_cancelled_and_no_result_of_its_own(
    tmp_path,
):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    agents[0].hold = True
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
    await agents[0].started.wait()
    await facade.handle_line(
        _line("session/cancel", {"sessionId": session_id}, notify=True)
    )
    await facade._sessions[session_id].prompt_task
    assert _by_id(writer, 3)["result"] == {"stopReason": "cancelled"}
    assert [item.get("id") for item in writer.sent if "id" in item] == [
        1,
        2,
        3,
    ]
    assert agents[0].stop_calls == 1
    await facade.shutdown()


async def test_cancel_request_and_exception_from_the_turn(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    agents[0].hold = True
    agents[0].error = RuntimeError("оборвалось")
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "a"}],
            },
            9,
        )
    )
    await agents[0].started.wait()
    await facade.handle_line(_line("$/cancel_request", {"id": 9}, notify=True))
    await facade._sessions[session_id].prompt_task
    assert _by_id(writer, 9)["result"]["stopReason"] == "cancelled"
    await facade.shutdown()


async def test_cancel_before_second_prompt_starts_skips_the_turn(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
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
    await facade._sessions[session_id].prompt_task
    assert len(agents[0].calls) == 1

    entered = asyncio.Event()
    release = asyncio.Event()
    run_prompt = facade._run_prompt

    async def hold_until_cancel(live, request_id, text):
        entered.set()
        await release.wait()
        await run_prompt(live, request_id, text)

    facade._run_prompt = hold_until_cancel
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "b"}],
            },
            4,
        )
    )
    await entered.wait()
    await facade.handle_line(
        _line("session/cancel", {"sessionId": session_id}, notify=True)
    )
    release.set()
    await facade._sessions[session_id].prompt_task
    assert len(agents[0].calls) == 1
    assert _by_id(writer, 4)["result"] == {"stopReason": "cancelled"}
    await facade.shutdown()


async def test_late_cancel_does_not_rewrite_stop_reason(tmp_path):
    facade, _writer, agents = _open(tmp_path)
    writer = GatedWriter()
    facade._writer = writer
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    writer.gate = True
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
    await writer.reached.wait()
    await facade.handle_line(
        _line("session/cancel", {"sessionId": session_id}, notify=True)
    )
    writer.release.set()
    await facade._sessions[session_id].prompt_task
    assert _by_id(writer, 3)["result"]["stopReason"] == "end_turn"
    await facade.shutdown()


async def test_image_does_not_start_a_turn(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    await facade.handle_line(
        _line(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "image", "data": "qq"}],
            },
            3,
        )
    )
    assert _by_id(writer, 3)["error"]["code"] == -32602
    assert agents[0].calls == []
    await facade.shutdown()


async def test_token_limit_and_final_answer_without_deltas(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    agents[0].deltas = []
    agents[0].answer = "итог"
    agents[0].outcome = "token_limit"
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
    await facade._sessions[session_id].prompt_task
    assert _updates(writer)[-1]["content"]["text"] == "итог"
    assert _by_id(writer, 3)["result"]["stopReason"] == "max_tokens"
    await facade.shutdown()


async def test_run_error_is_internal(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    agents[0].error = RuntimeError("boom")
    agents[0].deltas = []
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
    await facade._sessions[session_id].prompt_task
    assert _by_id(writer, 3)["error"]["code"] == -32603
    await facade.shutdown()


async def test_ignored_client_fields_still_create_a_session(tmp_path, capsys):
    facade, writer, _agents = _open(tmp_path)
    await _init(facade)
    await _new(
        facade,
        "/work",
        mcpServers=[{"name": "from-client"}],
        additionalDirectories=["/other"],
    )
    err = capsys.readouterr().err
    assert "mcpServers" in err
    assert "additionalDirectories" in err
    assert "sessionId" in _by_id(writer, 2)["result"]
    await facade.shutdown()


async def test_relative_cwd_is_invalid_params(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await facade.handle_line(
        _line("session/new", {"cwd": "relative", "mcpServers": []}, 2)
    )
    assert _by_id(writer, 2)["error"]["code"] == -32602
    assert agents == []
    await facade.shutdown()


async def test_list_resume_load_after_restart(tmp_path):
    facade, writer, _agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    Thread(tmp_path, session_id).append(
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "здравствуйте"},
    )
    await facade.shutdown()

    restored, writer2, agents2 = _open(tmp_path)
    await _init(restored)
    await restored.handle_line(_line("session/list", {}, 2))
    sessions = _by_id(writer2, 2)["result"]["sessions"]
    assert sessions[0]["sessionId"] == session_id
    assert sessions[0]["cwd"] == "/work/app"
    assert sessions[0]["title"] == "привет"
    assert "nextCursor" not in _by_id(writer2, 2)["result"]

    await restored.handle_line(
        _line(
            "session/load",
            {"sessionId": "missing", "cwd": "/work/app", "mcpServers": []},
            3,
        )
    )
    assert _by_id(writer2, 3)["error"]["code"] == -32002
    await restored.handle_line(
        _line(
            "session/resume",
            {"sessionId": session_id, "cwd": "/other"},
            4,
        )
    )
    assert _by_id(writer2, 4)["error"]["code"] == -32002

    await restored.handle_line(
        _line(
            "session/resume",
            {"sessionId": session_id, "cwd": "/work/app"},
            5,
        )
    )
    assert _by_id(writer2, 5)["result"] == {}
    assert len(agents2) == 1
    await restored.handle_line(
        _line(
            "session/resume",
            {"sessionId": session_id, "cwd": "/work/app"},
            6,
        )
    )
    assert len(agents2) == 1

    await restored.handle_line(
        _line(
            "session/load",
            {"sessionId": session_id, "cwd": "/work/app", "mcpServers": []},
            7,
        )
    )
    updates = _updates(writer2)
    assert [item["sessionUpdate"] for item in updates] == [
        "user_message_chunk",
        "agent_message_chunk",
    ]
    assert updates[0]["content"]["text"] == "привет"
    assert _by_id(writer2, 7)["result"] == {}
    # Проигрыш кончился до ответа load.
    load_at = next(
        index
        for index, item in enumerate(writer2.sent)
        if item.get("id") == 7 and "result" in item
    )
    assert any(
        item.get("method") == "session/update"
        for item in writer2.sent[:load_at]
    )
    await restored.shutdown()


async def test_load_replays_a_message_written_twice_once(tmp_path):
    """Тред develop с повтором строки (повтор шага): ``session/load``
    проигрывает сообщение один раз — как develop."""
    from gc_core.threads import file_layout as layout

    facade, writer, _agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work/app")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    Thread(tmp_path, session_id).append(
        {"id": "u1", "role": "user", "content": "привет"}
    )
    data = Path(layout.history_file(str(tmp_path), "acp", session_id))
    lines = data.read_text(encoding="utf-8").splitlines(keepends=True)
    data.write_text("".join([*lines, lines[-1]]), encoding="utf-8")
    await facade.shutdown()

    restored, writer2, _agents2 = _open(tmp_path)
    await _init(restored)
    await restored.handle_line(
        _line(
            "session/load",
            {"sessionId": session_id, "cwd": "/work/app", "mcpServers": []},
            3,
        )
    )
    updates = _updates(writer2)
    assert [item["sessionUpdate"] for item in updates] == [
        "user_message_chunk"
    ]
    await restored.shutdown()


async def test_delete_removes_acp_thread_and_repeat_is_not_found(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    thread = Thread(tmp_path, session_id)
    thread.append({"role": "user", "content": "hi"})
    cli_thread = Thread(tmp_path, "foreign", system="cli")
    cli_thread.append({"role": "user", "content": "чужой"})
    agents[0].hold = True
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
    await agents[0].started.wait()
    await facade.handle_line(
        _line("session/delete", {"sessionId": session_id}, 4)
    )
    assert _by_id(writer, 4)["result"] == {}
    await _drain(facade)
    assert _by_id(writer, 3)["result"]["stopReason"] == "cancelled"
    prompt_at = next(
        index for index, item in enumerate(writer.sent) if item.get("id") == 3
    )
    delete_at = next(
        index for index, item in enumerate(writer.sent) if item.get("id") == 4
    )
    assert delete_at < prompt_at
    assert agents[0].closed is True
    assert await facade._index.load(session_id) is None
    assert not thread.exists()
    await facade.handle_line(
        _line("session/delete", {"sessionId": session_id}, 5)
    )
    assert _by_id(writer, 5)["error"]["code"] == -32002
    await facade.handle_line(
        _line("session/delete", {"sessionId": "foreign"}, 6)
    )
    assert _by_id(writer, 6)["error"]["code"] == -32002
    assert cli_thread.exists()
    await facade.shutdown()


async def test_close_drops_the_agent_and_keeps_files(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    await facade.handle_line(
        _line("session/close", {"sessionId": session_id}, 3)
    )
    assert _by_id(writer, 3)["result"] == {}
    assert agents[0].closed is True
    assert await facade._index.load(session_id) is not None
    await facade.handle_line(
        _line("session/close", {"sessionId": session_id}, 4)
    )
    assert _by_id(writer, 4)["error"]["code"] == -32602
    await facade.shutdown()


async def test_unknown_method_and_parse_error(tmp_path, capsys):
    facade, writer, _agents = _open(tmp_path)
    await _init(facade)
    await facade.handle_line(_line("fs/read_text_file", {}, 2))
    assert _by_id(writer, 2)["error"]["code"] == -32601
    await facade.handle_line('{"id": 8, "method":')
    assert _by_id(writer, 8)["error"]["code"] == -32700
    sent_before = len(writer.sent)
    await facade.handle_line("not-json")
    assert len(writer.sent) == sent_before
    assert "parse error" in capsys.readouterr().err
    await facade.shutdown()


async def test_close_during_a_hung_turn_answers_and_serves_the_next_line(
    tmp_path,
):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    agents[0].hold = True
    agents[0].release_on_stop = False
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
    await agents[0].started.wait()
    await facade.handle_line(
        _line("session/close", {"sessionId": session_id}, 4)
    )
    assert _by_id(writer, 4)["result"] == {}
    assert not any(item.get("id") == 3 for item in writer.sent)
    assert agents[0].closed is False
    assert agents[0].stop_calls == 1
    await facade.handle_line(_line("session/list", {}, 5))
    assert "sessions" in _by_id(writer, 5)["result"]
    agents[0].release.set()
    await _drain(facade)
    assert agents[0].closed is True
    assert _by_id(writer, 3)["result"]["stopReason"] == "cancelled"
    assert await facade._index.load(session_id) is not None
    await facade.shutdown()


async def test_delete_during_a_hung_turn_answers_and_serves_the_next_line(
    tmp_path,
):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    thread = Thread(tmp_path, session_id)
    thread.append({"role": "user", "content": "hi"})
    agents[0].hold = True
    agents[0].release_on_stop = False
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
    await agents[0].started.wait()
    await facade.handle_line(
        _line("session/delete", {"sessionId": session_id}, 4)
    )
    assert _by_id(writer, 4)["result"] == {}
    assert not any(item.get("id") == 3 for item in writer.sent)
    assert agents[0].closed is False
    assert await facade._index.load(session_id) is None
    assert thread.messages() == 1
    await facade.handle_line(
        _line("session/delete", {"sessionId": session_id}, 5)
    )
    assert _by_id(writer, 5)["error"]["code"] == -32002
    agents[0].release.set()
    await _drain(facade)
    assert agents[0].closed is True
    assert not thread.exists()
    assert _by_id(writer, 3)["result"]["stopReason"] == "cancelled"
    delete_at = next(
        index for index, item in enumerate(writer.sent) if item.get("id") == 4
    )
    prompt_at = next(
        index for index, item in enumerate(writer.sent) if item.get("id") == 3
    )
    assert delete_at < prompt_at
    await facade.shutdown()


async def test_shutdown_does_not_wait_for_a_hung_turn(tmp_path):
    facade, writer, agents = _open(tmp_path)
    await _init(facade)
    await _new(facade, "/work")
    session_id = _by_id(writer, 2)["result"]["sessionId"]
    agents[0].hold = True
    agents[0].release_on_stop = False
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
    await agents[0].started.wait()
    await facade.shutdown()
    assert agents[0].stop_calls == 1
    assert agents[0].closed is False
    assert not any(item.get("id") == 3 for item in writer.sent)
    agents[0].release.set()
    for _ in range(5):
        if any(item.get("id") == 3 for item in writer.sent):
            break
        await asyncio.sleep(0)
    assert _by_id(writer, 3)["result"]["stopReason"] == "cancelled"
    assert agents[0].closed is False


async def _drain(facade):
    pending = list(facade._releasing)
    if pending:
        await asyncio.gather(*pending)
