"""S1: один turn-финализатор на ход — лишние tool_calls после первого финализатора
не исполняются (получают synthetic skip-output)."""
from processing_responses import split_at_first_finalizer


def test_split_stops_after_first_finalizer():
    calls = [{"name": "read_file"}, {"name": "turn_ask_human"}, {"name": "turn_ask_human"}]
    executed, skipped = split_at_first_finalizer(calls)
    assert executed == calls[:2]      # read + первый ask_human
    assert skipped == calls[2:]       # второй ask_human


def test_split_finish_then_action():
    calls = [{"name": "turn_finish"}, {"name": "read_file"}]
    executed, skipped = split_at_first_finalizer(calls)
    assert executed == calls[:1]      # стоп после turn_finish
    assert skipped == calls[1:]


def test_split_intermediate_not_finalizer():
    calls = [{"name": "turn_intermediate_reply"}, {"name": "turn_intermediate_reply"}]
    executed, skipped = split_at_first_finalizer(calls)
    assert executed == calls          # оба исполняются (не финализаторы)
    assert skipped == []


def test_split_no_finalizer():
    calls = [{"name": "read_file"}, {"name": "search"}]
    executed, skipped = split_at_first_finalizer(calls)
    assert executed == calls
    assert skipped == []


def test_process_tool_calls_skips_second_finalizer(monkeypatch):
    """2× turn_ask_human → apply_tool вызван 1 раз; у 2-го — synthetic skip-output."""
    import processing_responses as pr

    calls = []
    monkeypatch.setattr(pr, "apply_tool",
                        lambda **kw: calls.append(kw["tool_name"]) or '{"ok": true}')
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output",
                        lambda res, **kw: res)
    monkeypatch.setattr(pr, "maybe_update_intent_scope_from_tool", lambda **kw: None)
    monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")

    class _State:
        messages = []
        def __init__(self): self.session_compact_applied = False

    messages = [
        {"type": "function_call", "name": "turn_ask_human", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call", "name": "turn_ask_human", "call_id": "c2", "arguments": "{}"},
    ]
    ctx = type("C", (), {"config_dict": {}, "logger": None})()
    outs = pr.process_tool_calls(messages, _State(), ctx, run_agent_func=None)
    assert calls == ["turn_ask_human"]                 # apply_tool — только для 1-го
    assert len(outs) == 2
    assert "skipped" in outs[1]["output"] and outs[1]["call_id"] == "c2"


def test_flush_non_blocking_outputs_before_ask_human(monkeypatch):
    """Не-блокирующие output'ы (session_memory) персистятся ДО запуска блокирующего
    turn_ask_human — иначе висят 'running', пока ждём ответ юзера."""
    import processing_responses as pr

    persisted = []
    order = []

    def fake_apply(**kw):
        order.append(kw["tool_name"])
        if kw["tool_name"] == "turn_ask_human":
            flushed = [m for batch in persisted for m in batch]
            assert any(m.get("call_id") == "c1" for m in flushed), \
                "session_memory output должен быть сфлашен ДО turn_ask_human"
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
            self.turn_tool_calls = 0

    messages = [
        {"type": "function_call", "name": "session_memory", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call", "name": "turn_ask_human", "call_id": "c2", "arguments": "{}"},
    ]
    ctx = type("C", (), {"config_dict": {}, "logger": None})()
    outs = pr.process_tool_calls(
        messages, _State(), ctx, run_agent_func=None,
        persist_messages_callback=lambda msgs: persisted.append(list(msgs)),
    )
    assert order == ["session_memory", "turn_ask_human"]
    assert any(m.get("call_id") == "c1" for m in outs)   # output не потерян в возврате


def test_flush_skipped_outputs_before_ask_human(monkeypatch):
    """Skip-output'ы пропущенных (после 1-го финализатора) тулов тоже флашатся до
    блокирующего turn_ask_human — иначе пропущенный тул висит 'running' до ответа."""
    import processing_responses as pr

    persisted = []

    def fake_apply(**kw):
        if kw["tool_name"] == "turn_ask_human":
            flushed = [m for batch in persisted for m in batch]
            assert any(m.get("call_id") == "c3" for m in flushed), \
                "skip-output пропущенного тула должен быть сфлашен ДО turn_ask_human"
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
            self.turn_tool_calls = 0

    messages = [
        {"type": "function_call", "name": "turn_ask_human", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call", "name": "session_memory", "call_id": "c3", "arguments": "{}"},  # skipped
    ]
    ctx = type("C", (), {"config_dict": {}, "logger": None})()
    outs = pr.process_tool_calls(
        messages, _State(), ctx, run_agent_func=None,
        persist_messages_callback=lambda msgs: persisted.append(list(msgs)),
    )
    assert any(m.get("call_id") == "c3" and "skipped" in m["output"] for m in outs)
