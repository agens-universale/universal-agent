import pytest

from gc_computer_mcp.config import ServerConfig, load_from_env


def test_defaults():
    cfg = ServerConfig()
    assert cfg.readonly is False
    assert cfg.enable_vision is False
    assert cfg.disabled_tools == frozenset()
    assert cfg.log_level == "INFO"
    assert cfg.session_id is None
    assert cfg.state_ttl_seconds == 3600


def test_load_from_env_readonly_true(monkeypatch):
    monkeypatch.setenv("GC_COMPUTER_MCP_READONLY", "true")
    cfg = load_from_env()
    assert cfg.readonly is True


def test_load_from_env_disabled_tools_csv(monkeypatch):
    monkeypatch.setenv("GC_COMPUTER_MCP_DISABLED_TOOLS", "drag,shortcut")
    cfg = load_from_env()
    assert cfg.disabled_tools == frozenset({"drag", "shortcut"})


def test_load_from_env_log_level(monkeypatch):
    monkeypatch.setenv("GC_COMPUTER_MCP_LOG_LEVEL", "DEBUG")
    cfg = load_from_env()
    assert cfg.log_level == "DEBUG"


def test_load_from_env_session_id(monkeypatch):
    monkeypatch.setenv("GC_COMPUTER_MCP_SESSION_ID", "abc-123")
    cfg = load_from_env()
    assert cfg.session_id == "abc-123"


def test_load_from_env_rejects_invalid_bool(monkeypatch):
    monkeypatch.setenv("GC_COMPUTER_MCP_READONLY", "maybe")
    with pytest.raises(ValueError):
        load_from_env()


def test_frozen_attr_assignment_raises():
    import dataclasses

    cfg = ServerConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.readonly = True  # type: ignore[misc]


def test_timeouts_are_immutable():
    cfg = ServerConfig()
    with pytest.raises(TypeError):
        cfg.timeouts["click"] = 999.0  # type: ignore[index]


def test_default_timeouts_constant_is_immutable():
    from gc_computer_mcp.config import DEFAULT_TIMEOUTS

    with pytest.raises(TypeError):
        DEFAULT_TIMEOUTS["click"] = 999.0  # type: ignore[index]


def test_load_from_env_rejects_invalid_log_level(monkeypatch):
    monkeypatch.setenv("GC_COMPUTER_MCP_LOG_LEVEL", "WHAT")
    with pytest.raises(ValueError):
        load_from_env()


def test_load_from_env_rejects_zero_or_negative_ttl(monkeypatch):
    monkeypatch.setenv("GC_COMPUTER_MCP_STATE_TTL", "0")
    with pytest.raises(ValueError):
        load_from_env()
    monkeypatch.setenv("GC_COMPUTER_MCP_STATE_TTL", "-5")
    with pytest.raises(ValueError):
        load_from_env()


def test_load_from_env_rejects_non_integer_ttl(monkeypatch):
    monkeypatch.setenv("GC_COMPUTER_MCP_STATE_TTL", "not-a-number")
    with pytest.raises(ValueError):
        load_from_env()


def test_empty_env_matches_defaults(monkeypatch):
    for name in [
        "GC_COMPUTER_MCP_READONLY",
        "GC_COMPUTER_MCP_ENABLE_VISION",
        "GC_COMPUTER_MCP_DISABLED_TOOLS",
        "GC_COMPUTER_MCP_LOG_LEVEL",
        "GC_COMPUTER_MCP_LOG_DIR",
        "GC_COMPUTER_MCP_SESSION_ID",
        "GC_COMPUTER_MCP_STATE_TTL",
    ]:
        monkeypatch.delenv(name, raising=False)
    cfg = load_from_env()
    assert cfg == ServerConfig()
