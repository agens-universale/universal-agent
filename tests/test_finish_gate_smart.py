"""Backend smart (спека §3): судья при >=1 вызове инструмента за ход.
Счётчик — State.turn_tool_calls; проекция — apply_finish_gate_mode;
чтение — FinishGateGuard.enabled. CLI/server ключ finish_gate_smart не ставят."""
import pytest
from types import SimpleNamespace


def test_schema_default_is_smart():
    from config_modules.tui import TuiConfig
    cfg = TuiConfig()
    assert cfg.finish_gate_mode == "smart"
    assert cfg.text_answer_mode == "judge"


def test_schema_accepts_smart():
    from config_modules.tui import TuiConfig
    assert TuiConfig(finish_gate_mode="smart").finish_gate_mode == "smart"
    assert TuiConfig(finish_gate_mode="on").finish_gate_mode == "on"
    assert TuiConfig(finish_gate_mode="off").finish_gate_mode == "off"


def test_schema_rejects_unknown_mode():
    from config_modules.tui import TuiConfig
    with pytest.raises(Exception):
        TuiConfig(finish_gate_mode="maybe")


def test_state_counter_starts_zero_and_resets_on_flush():
    from states import State
    st = State.__new__(State)          # без тяжёлого __init__ (get_project_data и т.п.)
    st.turn_tool_calls = 0
    st.current_user_intent = []
    st.user_intent_log = []
    st.user_turn_seq = 0
    st.turn_tool_calls = 5
    st.flush_current_user_intent()
    assert st.turn_tool_calls == 0     # сброс на границе хода


def test_apply_finish_gate_mode_projects_smart():
    from tui.toggles import apply_finish_gate_mode
    cfg = {"tui_finish_gate_mode": "smart", "tui_finish_gate_mode_explicit": True}
    apply_finish_gate_mode(cfg)
    assert cfg["strict_turn_finish_validation"] is True     # судья потенциально включён
    assert cfg["finish_gate_smart"] is True


def test_apply_finish_gate_mode_on_off_unchanged():
    from tui.toggles import apply_finish_gate_mode
    on = {"tui_finish_gate_mode": "on", "tui_finish_gate_mode_explicit": True}
    apply_finish_gate_mode(on)
    assert on["strict_turn_finish_validation"] is True and on["finish_gate_smart"] is False
    off = {"tui_finish_gate_mode": "off", "tui_finish_gate_mode_explicit": True}
    apply_finish_gate_mode(off)
    assert off["strict_turn_finish_validation"] is False and off["finish_gate_smart"] is False


def _guard(cfg, turn_tool_calls):
    from intent_guard.guard import FinishGateGuard
    ctx = SimpleNamespace(config_dict=cfg)
    state = SimpleNamespace(parent_state_id=None, turn_tool_calls=turn_tool_calls)
    return FinishGateGuard(ctx=ctx, state=state)


def test_smart_skips_judge_without_tool_calls():
    cfg = {"backend_api_mode": "chat_completions",
           "strict_turn_finish_validation": True, "finish_gate_smart": True}
    assert _guard(cfg, 0).enabled is False
    assert _guard(cfg, 1).enabled is True


def test_cli_server_unaffected_no_smart_key():
    """CLI/server: apply_finish_gate_mode не зовётся → ключа finish_gate_smart нет →
    guard работает как прежде (strict AND chat_completions), счётчик не читается."""
    cfg = {"backend_api_mode": "chat_completions", "strict_turn_finish_validation": True}
    assert _guard(cfg, 0).enabled is True     # 0 вызовов не гасит судью без smart-ключа
    off = {"backend_api_mode": "chat_completions", "strict_turn_finish_validation": False}
    assert _guard(off, 5).enabled is False
