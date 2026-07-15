"""I2/C: present→ack→wait|deny; TraceScreen; always cancel; confirm-in-tool flush."""
from __future__ import annotations

import threading
import types

import pytest


def test_present_confirm_targets_trace_screen_and_returns_ack(monkeypatch):
    """_present_confirm ищет TraceScreen в стеке и возвращает mounted ack."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp()
    calls: list[tuple] = []
    fake_trace = types.SimpleNamespace(
        show_confirm_banner=lambda t, q, f: calls.append((t, q, f)),
    )
    monkeypatch.setattr(app, "_main_screen", lambda: fake_trace, raising=False)

    def sync_call_from_thread(fn):
        return fn()

    monkeypatch.setattr(app, "call_from_thread", sync_call_from_thread, raising=False)

    assert app._present_confirm("Title", "Question?", [("Path", "/x")]) is True
    assert calls == [("Title", "Question?", [("Path", "/x")])]


def test_present_confirm_returns_false_without_trace_screen(monkeypatch):
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp()
    monkeypatch.setattr(app, "_main_screen", lambda: None, raising=False)
    monkeypatch.setattr(app, "call_from_thread", lambda fn: fn(), raising=False)
    assert app._present_confirm("t", "q?", []) is False


def test_tui_confirm_denies_without_wait_when_present_fails():
    """Present fail → wait не вызывается, сразу deny."""
    from tui.services.agent_runner import AgentRunner

    runner = AgentRunner(
        executor=types.SimpleNamespace(stop_run=lambda: None, add_message=lambda m: None)
    )
    waited = {"n": 0}
    orig_wait = runner._confirm_event.wait

    def counting_wait(*a, **kw):
        waited["n"] += 1
        return orig_wait(*a, **kw)

    runner._confirm_event.wait = counting_wait  # type: ignore[method-assign]
    confirm = runner.make_tui_confirm(lambda *_a: False)
    assert confirm(title="t", question="q", fields=[]) == "deny"
    assert waited["n"] == 0


def test_tui_confirm_waits_only_after_mounted_ack():
    from tui.services.agent_runner import AgentRunner

    runner = AgentRunner(
        executor=types.SimpleNamespace(stop_run=lambda: None, add_message=lambda m: None)
    )
    result: list[str] = []

    def present(_t, _q, _f) -> bool:
        threading.Timer(0.05, lambda: runner.submit_confirm_reply("once")).start()
        return True

    confirm = runner.make_tui_confirm(present)

    def worker():
        result.append(confirm(title="t", question="q", fields=[]))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=3.0)
    assert not t.is_alive()
    assert result == ["once"]


@pytest.mark.asyncio
async def test_cancel_confirm_always_on_reset_even_without_banner():
    """stream_reset / reset: cancel_confirm всегда, даже если баннер не монтировался."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    cancelled = {"n": 0}
    runner = types.SimpleNamespace(
        cancel_confirm=lambda: cancelled.__setitem__("n", cancelled["n"] + 1),
        is_running=lambda: False,
    )
    async with TuiApp(config={"model": "M"}).run_test() as pilot:
        scr = pilot.app.screen
        pilot.app.active_runner = runner
        scr._confirm_active = False
        scr._reset_render_state()
        assert cancelled["n"] == 1


def test_tui_without_tui_confirm_denies_not_cli_input(monkeypatch):
    """is_tui без рабочего present → deny, не CLI input() за Textual."""
    from security import confirm_port as cp

    prompted = {"n": 0}

    def boom(**_kw):
        prompted["n"] += 1
        return "once"

    monkeypatch.setattr(cp, "prompt_user_confirmation_choice", boom)
    ctx = types.SimpleNamespace(is_tui=True, tui_confirm=None, config_dict={})
    assert cp.request_confirm(title="t", question="q?", fields=[], ctx=ctx) is False
    assert prompted["n"] == 0


def test_flush_outputs_before_confirm_blocking_tool(monkeypatch):
    """Батч: call1 ok + call2 confirm-block → до wait в jsonl есть output call1."""
    import processing_responses as pr

    persisted: list = []
    order: list[str] = []

    def fake_apply(**kw):
        order.append(kw["tool_name"])
        if kw["tool_name"] == "exec_python":
            flushed = [m for batch in persisted for m in batch]
            assert any(m.get("call_id") == "c1" for m in flushed), (
                "session_memory output должен быть сфлашен ДО confirm-in-tool"
            )
        return '{"ok": true}'

    monkeypatch.setattr(pr, "apply_tool", fake_apply)
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res)
    monkeypatch.setattr(pr, "maybe_update_intent_scope_from_tool", lambda **kw: None)
    monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")

    class _State:
        messages = []
        discard_writes_after_stop = False
        stop_requested = False

        def __init__(self):
            self.session_compact_applied = False

    messages = [
        {
            "type": "function_call",
            "name": "session_memory",
            "call_id": "c1",
            "arguments": "{}",
        },
        {
            "type": "function_call",
            "name": "exec_python",
            "call_id": "c2",
            "arguments": "{}",
        },
    ]
    ctx = type("C", (), {"config_dict": {}, "logger": None})()
    outs = pr.process_tool_calls(
        messages,
        _State(),
        ctx,
        run_agent_func=None,
        persist_messages_callback=lambda msgs: persisted.append(list(msgs)),
    )
    assert order == ["session_memory", "exec_python"]
    assert any(m.get("call_id") == "c1" for m in outs)
