from gc_core.security.confirm_port import _ask_human_enabled
from gc_testkit.settings import fake_ctx


def test_ask_human_disabled_when_turn_ask_human_in_stop_tools():
    ctx = fake_ctx(config={"stop_tools": ["turn_ask_human"]})
    assert _ask_human_enabled(ctx) is False


def test_ask_human_enabled_when_not_stopped():
    ctx = fake_ctx(config={"stop_tools": []})
    assert _ask_human_enabled(ctx) is True
