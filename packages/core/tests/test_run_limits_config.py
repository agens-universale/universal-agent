import pytest
from pydantic import ValidationError

from gc_core.config_modules.agent import AgentConfig


def test_limits_default_none():
    cfg = AgentConfig()
    assert cfg.session_token_limit is None
    assert cfg.max_tool_calls is None


def test_limits_accept_positive_int():
    cfg = AgentConfig(session_token_limit=100000, max_tool_calls=50)
    assert cfg.session_token_limit == 100000
    assert cfg.max_tool_calls == 50


@pytest.mark.parametrize("field", ["session_token_limit", "max_tool_calls"])
@pytest.mark.parametrize("bad", [0, -5, "many"])
def test_limits_reject_invalid(field, bad):
    with pytest.raises(ValidationError):
        AgentConfig(**{field: bad})


def test_limits_reach_flat_config_dict():
    from gc_core.config import flatten_app_config
    from gc_core.config_modules.root import AppConfig

    app_cfg = AppConfig(agent={"session_token_limit": 12345, "max_tool_calls": 7})
    flat = flatten_app_config(app_cfg)
    assert flat["session_token_limit"] == 12345
    assert flat["max_tool_calls"] == 7


def test_limits_flat_default_none():
    from gc_core.config import flatten_app_config
    from gc_core.config_modules.root import AppConfig

    flat = flatten_app_config(AppConfig())
    assert flat["session_token_limit"] is None
    assert flat["max_tool_calls"] is None
