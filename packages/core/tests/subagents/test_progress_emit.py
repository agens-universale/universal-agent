"""Step 3: child process_tool_calls emits tool_start/tool_end (and intermediate_reply)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from gc_core.tools.subagents.progress import (
    QueueSubagentProgressSink,
    SubagentProgressEvent,
)
from gc_testkit import run_coro


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


def _child_ctx(sink: QueueSubagentProgressSink):
    return SimpleNamespace(
        config_dict={},
        run_id="child_run_9",
        parent_call_id="parent_fc_1",
        subagent_progress_sink=sink,
        logger=None,
    )


def _parent_ctx(sink: QueueSubagentProgressSink | None = None):
    """Parent has sink for TUI drain but no parent_call_id — must not emit."""
    return SimpleNamespace(
        config_dict={},
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
        ctx = SimpleNamespace(
            config_dict={},
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
