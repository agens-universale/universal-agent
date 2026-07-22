import pytest
from pydantic import ValidationError

from config_modules.mcp import McpServerConfig


def _srv(**extra):
    return McpServerConfig(transport="stdio", command="x", **extra)


def test_whitelist_absent_by_default():
    assert _srv().whitelist is None


def test_whitelist_parses_and_normalizes():
    cfg = _srv(whitelist={"enabled": True, "tools": [" a ", "b", "a", ""]})
    assert cfg.whitelist.enabled is True
    assert cfg.whitelist.tools == ["a", "b"]  # trim, дедуп, пустые выброшены


def test_whitelist_enabled_defaults_false():
    cfg = _srv(whitelist={"tools": ["a"]})
    assert cfg.whitelist.enabled is False


def test_whitelist_unknown_key_forbidden():
    with pytest.raises(ValidationError):
        _srv(whitelist={"enabled": True, "tool": ["a"]})


def test_whitelist_tools_must_be_list():
    with pytest.raises(ValidationError):
        _srv(whitelist={"enabled": True, "tools": "a"})


def test_whitelist_survives_config_dump():
    cfg = _srv(whitelist={"enabled": True, "tools": ["a"]})
    dumped = cfg.model_dump(by_alias=True)
    assert dumped["whitelist"] == {"enabled": True, "tools": ["a"]}
