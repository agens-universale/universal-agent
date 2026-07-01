"""compact_thread: грузит тред + форс-сжатие через координатор."""
import types

import agent_executor as ae


def test_compact_thread_calls_force_compact(monkeypatch, tmp_path):
    captured = {}

    class _TS:
        history_messages = [{"role": "user", "content": "привет", "id": "u1"}]
        meta = {}
        thread_id = "t1"
        system_id = "cli"
        threads_dir = str(tmp_path)

    class _Coord:
        def __init__(self, **kw):
            captured["coord_state"] = kw["state"]
        def force_compact_once(self, **kw):
            captured["forced"] = True
        def on_summary_applied(self, ev):
            pass

    class _FakeState:
        # Мокаем State, чтобы НЕ дёргать реальный конструктор (он зовёт
        # get_project_data → WMI-хэнг на Windows). Capture messages.
        def __init__(self, **kw):
            self.messages = []
            self.backend_api_mode = "responses"
            self.shared = kw.get("shared")
            self.thread_id = kw.get("thread_id")
            self.dialog_steps_offset = 0
        def init_user_intent_log(self, v): pass
        def init_current_user_intent(self, v): pass
        def init_user_turn_seq(self, v): pass
        def infer_user_turn_seq_if_missing(self): pass

    monkeypatch.setattr(ae, "ThreadSession", types.SimpleNamespace(from_config=lambda **kw: _TS()))
    monkeypatch.setattr(ae, "repair_tool_call_pairing", lambda m, **kw: m)
    monkeypatch.setattr(ae, "RunSummaryCoordinator", _Coord)
    monkeypatch.setattr(ae, "parse_summary_trigger_chars", lambda c: 5000)
    monkeypatch.setattr(ae, "State", _FakeState)

    ex = ae.AgentExecutor.__new__(ae.AgentExecutor)
    ex._ctx = types.SimpleNamespace(
        config_dict={"model": "M", "max_steps": 30, "system_prompt_id": "x",
                     "backend_api_mode": "chat_completions"},
        tools_spec=[], tools_description=[],
    )
    ex._lock = __import__("threading").Lock()
    ex._state = None
    ex._stop_pending = False

    ok = ex.compact_thread("t1", system="cli")
    assert ok is True
    assert captured.get("forced") is True
    # история треда загружена в state, по которому идёт сжатие
    assert any(m.get("content") == "привет" for m in captured["coord_state"].messages)


def test_compact_thread_noop_in_responses(monkeypatch, tmp_path):
    ex = ae.AgentExecutor.__new__(ae.AgentExecutor)
    ex._ctx = types.SimpleNamespace(
        config_dict={"model": "M", "max_steps": 30, "system_prompt_id": "x",
                     "backend_api_mode": "responses"},
        tools_spec=[], tools_description=[],
    )
    ex._lock = __import__("threading").Lock()
    assert ex.compact_thread("t1", system="cli") is False
