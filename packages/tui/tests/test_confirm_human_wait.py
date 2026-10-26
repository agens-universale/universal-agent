"""I2/C: present→ack→wait|deny; TraceScreen; always cancel; confirm-in-tool flush."""

from __future__ import annotations

from gc_testkit.tui_support import tui_settings
import threading
import time
import types

import pytest
from gc_testkit import run_coro


def _async_noop(fn):
    """Заглушка корутинного хука: ядро зовёт async-двойник напрямую."""

    async def _wrapped(*args, **kwargs):
        return fn(*args, **kwargs)

    return _wrapped


def test_present_confirm_targets_trace_screen_and_returns_ack(monkeypatch):
    """_present_confirm ищет TraceScreen в стеке и возвращает mounted ack."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp()
    calls: list[tuple] = []
    fake_trace = types.SimpleNamespace(
        show_confirm_banner=lambda t, q, f: calls.append((t, q, f)),
    )
    monkeypatch.setattr(app, "_main_screen", lambda: fake_trace, raising=False)

    def sync_call_from_thread(fn):
        return fn()

    monkeypatch.setattr(
        app, "call_from_thread", sync_call_from_thread, raising=False
    )

    assert app._present_confirm("Title", "Question?", [("Path", "/x")]) is True
    assert calls == [("Title", "Question?", [("Path", "/x")])]


def test_present_confirm_returns_false_without_trace_screen(monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp()
    monkeypatch.setattr(app, "_main_screen", lambda: None, raising=False)
    monkeypatch.setattr(
        app, "call_from_thread", lambda fn: fn(), raising=False
    )
    assert app._present_confirm("t", "q?", []) is False


def test_tui_confirm_denies_without_wait_when_present_fails():
    """Present fail → wait не вызывается, сразу deny."""
    from gc_tui.services.agent_runner import AgentRunner

    runner = AgentRunner(
        executor=types.SimpleNamespace(
            stop_run=lambda: None, add_message=lambda m: None
        )
    )
    waited = {"n": 0}
    orig_wait = runner._confirm_event.wait

    def counting_wait(*a, **kw):
        waited["n"] += 1
        return orig_wait(*a, **kw)

    runner._confirm_event.wait = counting_wait  # type: ignore[method-assign]
    confirm = runner.make_tui_confirm(lambda *_a: False)
    assert confirm(title="t", question="q", fields=[]) == "deny_once"
    assert waited["n"] == 0


def test_tui_confirm_waits_only_after_mounted_ack():
    from gc_tui.services.agent_runner import AgentRunner

    runner = AgentRunner(
        executor=types.SimpleNamespace(
            stop_run=lambda: None, add_message=lambda m: None
        )
    )
    result: list[str] = []

    def present(_t, _q, _f) -> bool:
        threading.Timer(
            0.05, lambda: runner.submit_confirm_reply("allow_once")
        ).start()
        return True

    confirm = runner.make_tui_confirm(present)

    def worker():
        result.append(confirm(title="t", question="q", fields=[]))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=3.0)
    assert not t.is_alive()
    assert result == ["allow_once"]


def test_tui_confirm_timeout_auto_deny():
    """O11: нет ответа за timeout → deny_once, поток не висит вечно."""
    from gc_tui.services.agent_runner import AgentRunner

    runner = AgentRunner(
        executor=types.SimpleNamespace(
            stop_run=lambda: None, add_message=lambda m: None
        )
    )
    result: list[str] = []
    confirm = runner.make_tui_confirm(lambda *_a: True, timeout_sec=0.05)

    def worker():
        result.append(confirm(title="t", question="q", fields=[]))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=3.0)
    assert not t.is_alive()
    assert result == ["deny_once"]


def test_tui_confirm_stop_requested_denies_without_full_wait():
    """stop_requested во время wait → deny_once быстрее полного timeout."""
    from gc_tui.services.agent_runner import AgentRunner

    runner = AgentRunner(
        executor=types.SimpleNamespace(
            stop_run=lambda: None, add_message=lambda m: None
        )
    )
    result: list[str] = []
    confirm = runner.make_tui_confirm(lambda *_a: True, timeout_sec=30.0)

    def worker():
        result.append(confirm(title="t", question="q", fields=[]))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    time.sleep(0.05)
    runner._stop_requested = True
    t.join(timeout=3.0)
    assert not t.is_alive()
    assert result == ["deny_once"]


@pytest.mark.asyncio
async def test_cancel_confirm_always_on_reset_even_without_banner():
    """stream_reset / reset: cancel_confirm всегда, даже если баннер не монтировался."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    cancelled = {"n": 0}
    runner = types.SimpleNamespace(
        cancel_confirm=lambda: cancelled.__setitem__("n", cancelled["n"] + 1),
        is_running=lambda: False,
    )
    async with TuiApp(
        settings=tui_settings({"agent.model": "M"})
    ).run_test() as pilot:
        scr = pilot.app.screen
        pilot.app.active_runner = runner
        scr._confirm_active = False
        scr._reset_render_state()
        assert cancelled["n"] == 1


def test_tui_without_tui_confirm_denies_not_cli_input(monkeypatch):
    """is_tui без рабочего present → deny, не CLI input() за Textual."""
    from gc_core.security import confirm_port as cp
    from gc_core.security import path_policy as pp

    prompted = {"n": 0}

    def boom(**_kw):
        prompted["n"] += 1
        return "once"

    # CLI fallback lives in path_policy; confirm_port imports it lazily.
    monkeypatch.setattr(pp, "prompt_user_confirmation_choice", boom)
    from gc_testkit.settings import fake_ctx

    ctx = fake_ctx(is_tui=True, tui_confirm=None)
    assert (
        cp.request_confirm(title="t", question="q?", fields=[], ctx=ctx)
        is False
    )
    assert prompted["n"] == 0


def test_flush_outputs_before_confirm_blocking_tool(monkeypatch):
    """Батч: call1 ok + call2 confirm-block → до wait в jsonl есть output call1."""
    from gc_core import processing_responses as pr

    persisted: list = []
    order: list[str] = []

    async def fake_apply(**kw):
        order.append(kw["tool_name"])
        if kw["tool_name"] == "exec_python":
            flushed = [m for batch in persisted for m in batch]
            assert any(m.get("call_id") == "c1" for m in flushed), (
                "session_memory output должен быть сфлашен ДО confirm-in-tool"
            )
        return '{"ok": true}'

    monkeypatch.setattr(pr, "aapply_tool", fake_apply)
    monkeypatch.setattr(
        pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res
    )
    monkeypatch.setattr(
        pr,
        "amaybe_update_intent_scope_from_tool",
        _async_noop(lambda **kw: None),
    )
    monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")

    class _State:
        messages = []
        discard_writes_after_stop = False
        stop_requested = False

        def __init__(self):
            self.session_compact_applied = False
            self.tool_calls = 0
            self.turn_tool_calls = 0

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
    from gc_testkit.settings import fake_ctx

    ctx = fake_ctx(logger=None)
    outs = run_coro(
        pr.aprocess_tool_calls(
            messages,
            _State(),
            ctx,
            run_agent_func=None,
            persist_messages_callback=lambda msgs: persisted.append(
                list(msgs)
            ),
        )
    )
    assert order == ["session_memory", "exec_python"]
    assert any(m.get("call_id") == "c1" for m in outs)
