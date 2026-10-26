import pytest
from gc_core.config_modules.agent import AgentConfig
from pydantic import ValidationError


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


def test_limits_reach_app_config():
    from gc_core.config_modules.root import AppConfig

    app_cfg = AppConfig(
        agent={"session_token_limit": 12345, "max_tool_calls": 7}
    )
    assert app_cfg.agent.session_token_limit == 12345
    assert app_cfg.agent.max_tool_calls == 7


def test_limits_app_config_default_none():
    from gc_core.config_modules.root import AppConfig

    app_cfg = AppConfig()
    assert app_cfg.agent.session_token_limit is None
    assert app_cfg.agent.max_tool_calls is None
