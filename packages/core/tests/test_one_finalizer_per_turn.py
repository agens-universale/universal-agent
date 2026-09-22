"""Две кучи: work (не turn_finish) целиком, затем не больше одного turn_finish."""

from gc_core.processing_responses import partition_work_then_finish
from gc_testkit import run_coro


def _async_noop(fn):
    """Корутинная заглушка: ядро зовёт хуки и диспетчер только async-именами."""

    async def _wrapped(*args, **kwargs):
        return fn(*args, **kwargs)

    return _wrapped


def test_split_ask_human_defers_barrier_until_result_is_known():
    calls = [{"name": "read_file"}, {"name": "turn_ask_human"}, {"name": "turn_ask_human"}]
    executed, skipped = partition_work_then_finish(calls)
    assert executed == calls
    assert skipped == []


def test_split_finish_then_action():
    """Позиция turn_finish work не режет: сначала вся work, потом finish."""
    finish, read = {"name": "turn_finish"}, {"name": "read_file"}
    executed, skipped = partition_work_then_finish([finish, read])
    assert executed == [read, finish]
    assert skipped == []


def test_split_intermediate_not_finalizer():
    calls = [{"name": "turn_intermediate_reply"}, {"name": "turn_intermediate_reply"}]
    executed, skipped = partition_work_then_finish(calls)
    assert executed == calls  # оба исполняются (не финализаторы)
    assert skipped == []


def test_split_no_finalizer():
    calls = [{"name": "read_file"}, {"name": "search"}]
    executed, skipped = partition_work_then_finish(calls)
    assert executed == calls
    assert skipped == []


def test_process_tool_calls_skips_second_finish(monkeypatch):
    """2× turn_finish → apply_tool только у первого; у 2-го — synthetic skip."""
    from gc_core import processing_responses as pr

    calls = []
    monkeypatch.setattr(pr, "aapply_tool", _async_noop(lambda **kw: calls.append(kw["tool_name"]) or '{"ok": true}'))
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", _async_noop(lambda **kw: None))
    monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")

    class _State:
        messages = []

        def __init__(self):
            self.session_compact_applied = False
            self.tool_calls = 0

    messages = [
        {"type": "function_call", "name": "turn_finish", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call", "name": "turn_finish", "call_id": "c2", "arguments": "{}"},
    ]
    ctx = type("C", (), {"config_dict": {}, "logger": None})()
    outs = run_coro(pr.aprocess_tool_calls(messages, _State(), ctx, run_agent_func=None))
    assert calls == ["turn_finish"]
    assert len(outs) == 2
    assert "skipped" in outs[1]["output"] and outs[1]["call_id"] == "c2"


def test_flush_non_blocking_outputs_before_ask_human(monkeypatch):
    """Не-блокирующие output'ы (session_memory) персистятся ДО запуска блокирующего
    turn_ask_human — иначе висят 'running', пока ждём ответ юзера."""
    from gc_core import processing_responses as pr

    persisted = []
    order = []

    def fake_apply(**kw):
        order.append(kw["tool_name"])
        if kw["tool_name"] == "turn_ask_human":
            flushed = [m for batch in persisted for m in batch]
            assert any(m.get("call_id") == "c1" for m in flushed), (
                "session_memory output должен быть сфлашен ДО turn_ask_human"
            )
        return '{"ok": true}'

    monkeypatch.setattr(pr, "aapply_tool", _async_noop(fake_apply))
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", _async_noop(lambda **kw: None))
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
        {"type": "function_call", "name": "session_memory", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call", "name": "turn_ask_human", "call_id": "c2", "arguments": "{}"},
    ]
    ctx = type("C", (), {"config_dict": {}, "logger": None})()
    outs = run_coro(
        pr.aprocess_tool_calls(
            messages,
            _State(),
            ctx,
            run_agent_func=None,
            persist_messages_callback=lambda msgs: persisted.append(list(msgs)),
        )
    )
    assert order == ["session_memory", "turn_ask_human"]
    assert any(m.get("call_id") == "c1" for m in outs)  # output не потерян в возврате


def test_successful_ask_human_skips_later_finish(monkeypatch):
    """Успешный ask_human даёт skip-output следующему turn_finish."""
    from gc_core import processing_responses as pr

    calls = []

    def fake_apply(**kw):
        calls.append(kw["tool_name"])
        return '{"ok": true}'

    monkeypatch.setattr(pr, "aapply_tool", _async_noop(fake_apply))
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", _async_noop(lambda **kw: None))
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
        {"type": "function_call", "name": "turn_ask_human", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call", "name": "turn_finish", "call_id": "c_fin", "arguments": "{}"},
    ]
    ctx = type("C", (), {"config_dict": {}, "logger": None})()
    outs = run_coro(
        pr.aprocess_tool_calls(
            messages,
            _State(),
            ctx,
            run_agent_func=None,
        )
    )
    assert calls == ["turn_ask_human"]
    assert any(m.get("call_id") == "c_fin" and "skipped" in m["output"] for m in outs)
