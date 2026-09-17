"""Пресеты /rigor (спека §2, §6, §8): тройки = пары, резолвер, непересечение."""
import pytest


def test_presets_match_spec_table():
    from gc_tui.toggles import PRESETS
    assert PRESETS["low"] == {"tui.finish_gate_mode": "off", "tui.text_answer_mode": "allow"}
    assert PRESETS["medium"] == {"tui.finish_gate_mode": "smart", "tui.text_answer_mode": "judge"}
    assert PRESETS["high"] == {"tui.finish_gate_mode": "on", "tui.text_answer_mode": "deny"}


def test_presets_do_not_intersect():
    """Гвоздь §8: ни одна пара не совпадает с другой (иначе резолвер неоднозначен)."""
    from gc_tui.toggles import PRESETS
    pairs = [tuple(sorted(p.items())) for p in PRESETS.values()]
    assert len(set(pairs)) == len(pairs)


def test_resolve_preset_names_and_custom():
    from gc_tui.toggles import resolve_preset, PRESET_CUSTOM
    assert resolve_preset({"tui.finish_gate_mode": "off", "tui.text_answer_mode": "allow"}) == "low"
    assert resolve_preset({"tui.finish_gate_mode": "smart", "tui.text_answer_mode": "judge"}) == "medium"
    assert resolve_preset({"tui.finish_gate_mode": "on", "tui.text_answer_mode": "deny"}) == "high"
    # нестандартная пара → custom
    assert resolve_preset({"tui.finish_gate_mode": "on", "tui.text_answer_mode": "allow"}) == PRESET_CUSTOM


def test_resolve_rigor_state_reads_effective_pair():
    from gc_tui.toggles import resolve_rigor_state, PRESET_CUSTOM
    # medium: explicit smart + judge
    cfg = {
        "tui_finish_gate_mode": "smart", "tui_finish_gate_mode_explicit": True,
        "tui_text_answer_mode": "judge",
    }
    assert resolve_rigor_state(cfg) == "medium"
    # нестандартная сессионная пара on+allow → custom
    cfg2 = {
        "tui_finish_gate_mode": "on", "tui_finish_gate_mode_explicit": True,
        "tui_text_answer_mode": "allow",
    }
    assert resolve_rigor_state(cfg2) == PRESET_CUSTOM
    # дефолты (нет ключей) → smart + judge → medium
    assert resolve_rigor_state({}) == "medium"
    # low через явный off + allow
    cfg3 = {
        "tui_finish_gate_mode": "off", "tui_finish_gate_mode_explicit": True,
        "tui_text_answer_mode": "allow",
    }
    assert resolve_rigor_state(cfg3) == "low"
