"""agent_overrides swarm — та же семантика, что у CLI и webhook."""

import pytest
from gc_core.config_modules.path_hints import UnknownSettingError
from gc_server.swarm.runner import _apply_agent_overrides_to_raw


def test_overrides_replace_lists_and_expand_sections():
    raw = {
        "security": {
            "read_guard": {"mode": "deny", "allowed_read_roots": ["/file"]}
        },
        "mcp": {"servers": {"github": {"transport": "stdio", "command": "x"}}},
    }
    result = _apply_agent_overrides_to_raw(
        raw,
        {
            "security.read_guard.allowed_read_roots": ["/narrow"],
            "security": {"read_guard": {"mode": "ask"}},
            "mcp.servers": {},
        },
    )
    assert result["security"]["read_guard"] == {
        "mode": "ask",
        "allowed_read_roots": ["/narrow"],
    }
    assert result["mcp"]["servers"] == {}
    assert raw["security"]["read_guard"]["allowed_read_roots"] == ["/file"]


def test_unknown_flat_name_is_rejected_with_hint():
    with pytest.raises(UnknownSettingError, match="agent.max_steps"):
        _apply_agent_overrides_to_raw({}, {"max_steps": 5})
