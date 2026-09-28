from types import SimpleNamespace

from gc_core.security.confirm_port import _ask_human_enabled


def test_ask_human_disabled_when_turn_ask_human_in_stop_tools():
    ctx = SimpleNamespace(config_dict={"stop_tools": ["turn_ask_human"]})
    assert _ask_human_enabled(ctx) is False


def test_ask_human_enabled_when_not_stopped():
    ctx = SimpleNamespace(config_dict={"stop_tools": []})
    assert _ask_human_enabled(ctx) is True
