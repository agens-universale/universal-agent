"""Step 3: child process_tool_calls emits tool_start/tool_end (and intermediate_reply)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from gc_core.execution.progress import (
    QueueSubagentProgressSink,
    SubagentProgressEvent,
)
from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx


def _async_noop(fn):
    """Заглушка корутинного хука: ядро зовёт async-двойник напрямую."""

    async def _wrapped(*args, **kwargs):
        return fn(*args, **kwargs)

    return _wrapped


def test_tool_start_for_session_todo_carries_items():
    """processing_responses кладёт разобранные items в событие."""
    ev = SubagentProgressEvent(
        parent_call_id="parent_fc_1",
        child_run_id="child_run_9",
        kind="tool_start",
        step=1,
        max_steps=50,
        elapsed_ms=0,
        tool_name="session_todo",
        todo_items=[{"content": "a", "status": "pending"}],
    )
    assert ev.todo_items == [{"content": "a", "status": "pending"}]


def _state(*, steps=3, max_steps=50):
    return SimpleNamespace(
        messages=[],
        steps=steps,
        max_steps=max_steps,
        session_compact_applied=False,
        tool_calls=0,
        turn_tool_calls=0,
    )


_ROOT = "/work/project"


def _child_ctx(sink: QueueSubagentProgressSink):
    return fake_ctx(
        run_id="child_run_9",
        parent_call_id="parent_fc_1",
        subagent_progress_sink=sink,
        logger=None,
        workspace=SimpleNamespace(root=_ROOT),
    )


def _parent_ctx(sink: QueueSubagentProgressSink | None = None):
    """Parent has sink for TUI drain but no parent_call_id — must not emit."""
    return fake_ctx(
        run_id="parent_run",
        subagent_progress_sink=sink,
        logger=None,
    )


def _fc(name: str, call_id: str, arguments: str = "{}"):
    return {
        "id": f"id_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def _patch_process(monkeypatch, *, apply_return='{"ok": true}'):
    from gc_core import processing_responses as pr

    async def _fake_aapply_tool(**kw):
        return (
            apply_return if isinstance(apply_return, str) else apply_return(kw)
        )

    monkeypatch.setattr(pr, "aapply_tool", _fake_aapply_tool)
    monkeypatch.setattr(
        pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res
    )
    monkeypatch.setattr(
        pr,
        "amaybe_update_intent_scope_from_tool",
        _async_noop(lambda **kw: None),
    )
    monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")
    return pr


class TestEmitToolStartEnd:
    def test_child_emits_start_and_end(self, monkeypatch):
        pr = _patch_process(monkeypatch)
        sink = QueueSubagentProgressSink()
        outs = run_coro(
            pr.aprocess_tool_calls(
                messages=[_fc("fs_read_file", "c1", '{"path": "a.py"}')],
                state=_state(steps=4, max_steps=100),
                ctx=_child_ctx(sink),
                run_agent_func=None,
            )
        )
        assert len(outs) == 1
        events = sink.drain()
        kinds = [e.kind for e in events]
        assert kinds == ["tool_start", "tool_end"]

        start, end = events
        assert start.parent_call_id == "parent_fc_1"
        assert start.child_run_id == "child_run_9"
        assert start.tool_name == "fs_read_file"
        assert start.tool_call_id == "c1"
        assert start.step == 4
        assert start.max_steps == 100
        assert start.args_summary is not None
        assert "a.py" in (start.args_summary or "")

        assert end.ok is True
        assert end.tool_name == "fs_read_file"
        assert end.tool_call_id == "c1"
        assert end.elapsed_ms >= 0

    def test_tool_start_path_is_relative_to_the_workspace_root(
        self, monkeypatch
    ):
        pr = _patch_process(monkeypatch)
        sink = QueueSubagentProgressSink()
        args = json.dumps({"path": f"{_ROOT}/README.md"})
        run_coro(
            pr.aprocess_tool_calls(
                messages=[_fc("fs_read_file", "c1", args)],
                state=_state(),
                ctx=_child_ctx(sink),
                run_agent_func=None,
            )
        )
        start = sink.drain()[0]
        assert start.args_summary == "README.md"

    def test_parent_with_sink_does_not_emit(self, monkeypatch):
        pr = _patch_process(monkeypatch)
        sink = QueueSubagentProgressSink()
        run_coro(
            pr.aprocess_tool_calls(
                messages=[_fc("fs_read_file", "c1")],
                state=_state(),
                ctx=_parent_ctx(sink),
                run_agent_func=None,
            )
        )
        assert sink.drain() == []

    def test_child_without_sink_does_not_emit(self, monkeypatch):
        pr = _patch_process(monkeypatch)
        ctx = fake_ctx(
            run_id="child_run_9",
            parent_call_id="parent_fc_1",
            subagent_progress_sink=None,
            logger=None,
        )
        # no sink → no crash, no events
        run_coro(
            pr.aprocess_tool_calls(
                messages=[_fc("fs_read_file", "c1")],
                state=_state(),
                ctx=ctx,
                run_agent_func=None,
            )
        )

    def test_tool_end_ok_false_on_error_payload(self, monkeypatch):
        pr = _patch_process(
            monkeypatch,
            apply_return='{"ok": false, "error": {"message": "boom happened here"}}',
        )
        sink = QueueSubagentProgressSink()
        run_coro(
            pr.aprocess_tool_calls(
                messages=[_fc("fs_read_file", "c2")],
                state=_state(),
                ctx=_child_ctx(sink),
                run_agent_func=None,
            )
        )
        end = sink.drain()[1]
        assert end.kind == "tool_end"
        assert end.ok is False
        assert end.error_brief is not None
        assert "boom" in (end.error_brief or "")
        assert len(end.error_brief or "") <= 120

    def test_session_todo_tool_start_carries_todo_items(self, monkeypatch):
        pr = _patch_process(monkeypatch)
        sink = QueueSubagentProgressSink()
        run_coro(
            pr.aprocess_tool_calls(
                messages=[
                    _fc(
                        "session_todo",
                        "c1",
                        json.dumps(
                            {"todos": [{"content": "a", "status": "pending"}]}
                        ),
                    )
                ],
                state=_state(),
                ctx=_child_ctx(sink),
                run_agent_func=None,
            )
        )
        start = sink.drain()[0]
        assert start.todo_items == [{"content": "a", "status": "pending"}]

    def test_non_session_todo_tool_start_has_no_todo_items(self, monkeypatch):
        pr = _patch_process(monkeypatch)
        sink = QueueSubagentProgressSink()
        run_coro(
            pr.aprocess_tool_calls(
                messages=[_fc("fs_read_file", "c1", '{"path": "a.py"}')],
                state=_state(),
                ctx=_child_ctx(sink),
                run_agent_func=None,
            )
        )
        start = sink.drain()[0]
        assert start.todo_items is None

    def test_batch_emits_per_tool(self, monkeypatch):
        pr = _patch_process(monkeypatch)
        sink = QueueSubagentProgressSink()
        run_coro(
            pr.aprocess_tool_calls(
                messages=[
                    _fc("fs_read_file", "c1"),
                    _fc("fs_list_files", "c2"),
                ],
                state=_state(),
                ctx=_child_ctx(sink),
                run_agent_func=None,
            )
        )
        kinds = [e.kind for e in sink.drain()]
        assert kinds == ["tool_start", "tool_end", "tool_start", "tool_end"]

    def test_wrong_arguments_emits_tool_end_and_returns_error_output(
        self, monkeypatch
    ):
        from gc_core import processing_responses as pr

        # Берём класс У МОДУЛЯ, который его и ловит.
        #
        # `from gc_core.tools import ...` здесь ненадёжно: конфтесты тестов
        # подменяют `sys.modules["gc_core.tools"]` заглушкой и восстанавливают
        # обратно, и при неудачном порядке сборки получаются ДВА объекта класса
        # под одним именем — `except` перестаёт ловить, а тест падает на ровном
        # месте. Про эту опасность прямо написано в `subagents/conftest.py`.
        # Проверяем поведение цикла, а не устройство импортов, поэтому берём
        # класс оттуда, где он используется.
        WrongFunctionArgumentsException = pr.WrongFunctionArgumentsException

        async def _boom(**kw):
            raise WrongFunctionArgumentsException(
                "bad args", received_args="{}"
            )

        monkeypatch.setattr(pr, "aapply_tool", _boom)
        monkeypatch.setattr(
            pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res
        )
        monkeypatch.setattr(
            pr,
            "amaybe_update_intent_scope_from_tool",
            _async_noop(lambda **kw: None),
        )
        monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")

        sink = QueueSubagentProgressSink()
        outs = run_coro(
            pr.aprocess_tool_calls(
                messages=[_fc("fs_read_file", "c_bad")],
                state=_state(),
                ctx=_child_ctx(sink),
                run_agent_func=None,
            )
        )
        assert [o["call_id"] for o in outs] == ["c_bad"]
        assert (
            json.loads(outs[0]["output"])["error"]["type"]
            == "invalid_arguments"
        )
        events = sink.drain()
        assert [e.kind for e in events] == ["tool_start", "tool_end"]
        assert events[1].ok is False
        assert "bad args" in (events[1].error_brief or "")


class TestEmitIntermediateReply:
    def test_intermediate_reply_emits_prose_not_tool_pair(self, monkeypatch):
        answer = "Сейчас проверю структуру скилла"
        pr = _patch_process(
            monkeypatch,
            apply_return=json.dumps(
                {
                    "tool": "turn_intermediate_reply",
                    "ok": True,
                    "answer": answer,
                }
            ),
        )
        sink = QueueSubagentProgressSink()
        run_coro(
            pr.aprocess_tool_calls(
                messages=[
                    _fc(
                        "turn_intermediate_reply",
                        "ir1",
                        json.dumps({"answer": answer}),
                    )
                ],
                state=_state(),
                ctx=_child_ctx(sink),
                run_agent_func=None,
            )
        )
        events = sink.drain()
        assert [e.kind for e in events] == ["intermediate_reply"]
        assert events[0].prose == answer
        assert events[0].tool_name == "turn_intermediate_reply"
        assert events[0].ok is True


class TestToolEndResultFacts:
    def _run(self, monkeypatch, output):
        pr = _patch_process(monkeypatch, apply_return=output)
        sink = QueueSubagentProgressSink()
        run_coro(
            pr.aprocess_tool_calls(
                messages=[_fc("fs_read_file", "c1")],
                state=_state(),
                ctx=_child_ctx(sink),
                run_agent_func=None,
            )
        )
        return sink.drain()[-1]

    def test_status_of_failed_call(self, monkeypatch):
        end = self._run(
            monkeypatch, json.dumps({"ok": False, "status": "step_limit"})
        )
        assert end.kind == "tool_end"
        assert end.result_status == "step_limit"

    def test_mode_denial_error_fields(self, monkeypatch):
        end = self._run(
            monkeypatch,
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": "write_denied_by_mode",
                        "mode": "plan",
                        "reason": "r" * 300,
                    },
                }
            ),
        )
        assert end.result_error["type"] == "write_denied_by_mode"
        assert end.result_error["mode"] == "plan"
        # Факты без обрезки: живой вид равен возврату в сессию.
        assert end.result_error["reason"] == "r" * 300

    def test_success_has_no_facts(self, monkeypatch):
        end = self._run(monkeypatch, json.dumps({"ok": True}))
        assert end.result_status is None
        assert end.result_error is None

    def test_failed_intermediate_reply_event_keeps_brief(self, monkeypatch):
        pr = _patch_process(
            monkeypatch,
            apply_return=json.dumps(
                {
                    "ok": False,
                    "status": "step_limit",
                    "error": {"type": "x", "reason": "не принят"},
                }
            ),
        )
        sink = QueueSubagentProgressSink()
        run_coro(
            pr.aprocess_tool_calls(
                messages=[
                    _fc(
                        "turn_intermediate_reply",
                        "ir1",
                        json.dumps({"answer": "текст"}),
                    )
                ],
                state=_state(),
                ctx=_child_ctx(sink),
                run_agent_func=None,
            )
        )
        (event,) = sink.drain()
        assert event.ok is False
        assert event.error_brief == "не принят"
        # Факты исхода — только у `tool_end`: реплика — не вызов тула.
        assert event.result_status is None
        assert event.result_error is None


class TestToolStartCommand:
    """Сырой текст команды Run-тула — в событии начала вложенного вызова."""

    def _start(self, monkeypatch, name, arguments):
        pr = _patch_process(monkeypatch)
        sink = QueueSubagentProgressSink()
        run_coro(
            pr.aprocess_tool_calls(
                messages=[_fc(name, "c1", json.dumps(arguments))],
                state=_state(),
                ctx=_child_ctx(sink),
                run_agent_func=None,
            )
        )
        return sink.drain()[0]

    def test_multiline_command_rides_whole(self, monkeypatch):
        command = f"cd {_ROOT} && python - <<'EOF'\nprint(1)\nEOF"
        start = self._start(monkeypatch, "exec_command", {"command": command})
        assert start.kind == "tool_start"
        assert start.command == command

    def test_python_code_rides_whole(self, monkeypatch):
        code = "import json\nprint(json.dumps({'ok': 1}))\n"
        start = self._start(monkeypatch, "exec_python", {"code": code})
        assert start.command == code

    def test_command_at_the_cap_rides_whole(self, monkeypatch):
        from gc_core.execution.progress import TOOL_START_COMMAND_MAX

        command = "x" * TOOL_START_COMMAND_MAX
        start = self._start(monkeypatch, "exec_command", {"command": command})
        assert start.command == command

    def test_command_over_the_cap_is_none(self, monkeypatch):
        from gc_core.execution.progress import TOOL_START_COMMAND_MAX

        command = "x" * (TOOL_START_COMMAND_MAX + 1)
        start = self._start(monkeypatch, "exec_command", {"command": command})
        assert start.command is None

    def test_non_run_tool_has_no_command(self, monkeypatch):
        start = self._start(monkeypatch, "fs_read_file", {"path": "a.py"})
        assert start.command is None

    def test_sidecar_call_is_written_before_tool_start(self, monkeypatch):
        pr = _patch_process(monkeypatch)
        order: list[str] = []

        async def _persist(ctx, *, kind, payload):
            order.append(f"persist:{kind}")

        class _Sink:
            def emit(self, event):
                order.append(f"emit:{event.kind}")

        monkeypatch.setattr(pr, "apersist_subagent_trace", _persist)
        run_coro(
            pr.aprocess_tool_calls(
                messages=[
                    _fc("exec_command", "c1", json.dumps({"command": "ls"}))
                ],
                state=_state(),
                ctx=_child_ctx(_Sink()),
                run_agent_func=None,
            )
        )
        assert order.index("persist:call") < order.index("emit:tool_start")

    def test_top_level_emits_nothing(self, monkeypatch):
        pr = _patch_process(monkeypatch)
        sink = QueueSubagentProgressSink()
        run_coro(
            pr.aprocess_tool_calls(
                messages=[
                    _fc("exec_command", "c1", json.dumps({"command": "ls"}))
                ],
                state=_state(),
                ctx=_parent_ctx(sink),
                run_agent_func=None,
            )
        )
        assert sink.drain() == []


class TestToolEndOutput:
    """Сырой вывод вложенного вызова — в событии конца, целиком или никак."""

    def _run(self, monkeypatch, output, *, ctx_factory=None):
        pr = _patch_process(monkeypatch, apply_return=output)
        sink = QueueSubagentProgressSink()
        ctx = (ctx_factory or _child_ctx)(sink)
        run_coro(
            pr.aprocess_tool_calls(
                messages=[
                    _fc("exec_command", "c1", json.dumps({"command": "ls"}))
                ],
                state=_state(),
                ctx=ctx,
                run_agent_func=None,
            )
        )
        return sink.drain()

    def test_short_output_rides_whole(self, monkeypatch):
        envelope = json.dumps(
            {"ok": True, "exit_code": 0, "stdout": {"content": "a\nb\n"}}
        )
        end = self._run(monkeypatch, envelope)[-1]
        assert end.kind == "tool_end"
        assert end.output == envelope
        assert json.loads(end.output)["stdout"]["content"] == "a\nb\n"

    def test_output_at_the_cap_rides_whole(self, monkeypatch):
        from gc_core.execution.progress import TOOL_END_OUTPUT_MAX

        frame = '{"ok": true, "pad": ""}'
        pad = "x" * (TOOL_END_OUTPUT_MAX - len(frame))
        envelope = '{"ok": true, "pad": "' + pad + '"}'
        assert len(envelope) == TOOL_END_OUTPUT_MAX
        end = self._run(monkeypatch, envelope)[-1]
        assert end.output == envelope

    def test_output_over_the_cap_is_none(self, monkeypatch):
        from gc_core.execution.progress import TOOL_END_OUTPUT_MAX

        envelope = '{"ok": true, "pad": "' + "x" * TOOL_END_OUTPUT_MAX + '"}'
        end = self._run(monkeypatch, envelope)[-1]
        assert end.output is None

    def test_tool_start_has_no_output(self, monkeypatch):
        start = self._run(monkeypatch, '{"ok": true}')[0]
        assert start.output is None

    def test_sidecar_result_is_written_before_tool_end(self, monkeypatch):
        pr = _patch_process(monkeypatch, apply_return='{"ok": true}')
        order: list[str] = []
        persisted: list[dict] = []

        async def _persist(ctx, *, kind, payload):
            order.append(f"persist:{kind}")
            persisted.append(payload)

        class _Sink:
            def emit(self, event):
                order.append(f"emit:{event.kind}")

        monkeypatch.setattr(pr, "apersist_subagent_trace", _persist)
        run_coro(
            pr.aprocess_tool_calls(
                messages=[
                    _fc("exec_command", "c1", json.dumps({"command": "ls"}))
                ],
                state=_state(),
                ctx=_child_ctx(_Sink()),
                run_agent_func=None,
            )
        )
        assert order.index("persist:result") < order.index("emit:tool_end")
        assert persisted[-1]["output"] == '{"ok": true}'

    def test_sidecar_keeps_full_output_over_the_cap(self, monkeypatch):
        from gc_core.execution.progress import TOOL_END_OUTPUT_MAX

        envelope = '{"ok": true, "pad": "' + "x" * TOOL_END_OUTPUT_MAX + '"}'
        pr = _patch_process(monkeypatch, apply_return=envelope)
        persisted: list[dict] = []

        async def _persist(ctx, *, kind, payload):
            persisted.append(payload)

        monkeypatch.setattr(pr, "apersist_subagent_trace", _persist)
        run_coro(
            pr.aprocess_tool_calls(
                messages=[
                    _fc("exec_command", "c1", json.dumps({"command": "ls"}))
                ],
                state=_state(),
                ctx=_child_ctx(QueueSubagentProgressSink()),
                run_agent_func=None,
            )
        )
        assert persisted[-1]["output"] == envelope

    def test_top_level_emits_nothing(self, monkeypatch):
        events = self._run(
            monkeypatch, '{"ok": true}', ctx_factory=_parent_ctx
        )
        assert events == []

    def test_noop_sink_does_not_fail(self):
        from gc_core.execution.progress import (
            NOOP_SUBAGENT_PROGRESS_SINK,
            emit_progress,
        )

        ctx = fake_ctx(
            parent_call_id="p",
            subagent_progress_sink=NOOP_SUBAGENT_PROGRESS_SINK,
        )
        emit_progress(ctx, _state(), "tool_end", output='{"ok": true}')


def test_child_finish_start_summary_is_solution_type():
    """Сводка вызова субагента — по вложенной таблице отображения: у
    сдачи — `solution_type`."""
    from gc_core import processing_responses as pr

    sink = QueueSubagentProgressSink()
    args = json.dumps({"solution_type": "full_solution", "task": "сверить"})
    pr._emit_tool_start(_child_ctx(sink), _state(), "turn_finish", args, "f1")
    (start,) = sink.drain()
    assert start.args_summary == "full_solution"
