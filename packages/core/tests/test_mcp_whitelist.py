import pytest
from pydantic import ValidationError

from gc_core.config_modules.mcp import McpServerConfig


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


import logging

from gc_core.tools.connect_mcp import registry as reg


_RAW = [
    {"name": "search", "description": "s"},
    {"name": "create", "description": "c"},
    {"name": "delete", "description": "d"},
]


def test_filter_passthrough_without_block():
    assert reg._apply_tools_whitelist("srv", {"transport": "stdio"}, _RAW) == _RAW


def test_filter_passthrough_when_disabled():
    cfg = {"whitelist": {"enabled": False, "tools": ["search"]}}
    assert reg._apply_tools_whitelist("srv", cfg, _RAW) == _RAW


def test_filter_keeps_only_whitelisted():
    cfg = {"whitelist": {"enabled": True, "tools": ["search", "create"]}}
    out = reg._apply_tools_whitelist("srv", cfg, _RAW)
    assert [t["name"] for t in out] == ["search", "create"]


def test_filter_enabled_empty_means_no_tools():
    cfg = {"whitelist": {"enabled": True, "tools": []}}
    assert reg._apply_tools_whitelist("srv", cfg, _RAW) == []


def test_filter_warns_on_unknown_names(caplog):
    cfg = {"whitelist": {"enabled": True, "tools": ["search", "serch_typo"]}}
    with caplog.at_level(logging.WARNING):
        out = reg._apply_tools_whitelist("srv", cfg, _RAW)
    assert [t["name"] for t in out] == ["search"]
    warns = [r for r in caplog.records if "serch_typo" in r.getMessage()]
    assert len(warns) == 1
    assert "srv" in warns[0].getMessage()


def test_discover_one_applies_whitelist(monkeypatch):
    monkeypatch.setattr(reg, "list_tools_stream_stdio", lambda **kw: list(_RAW))
    server_cfg = {
        "transport": "stream_stdio",
        "command": "x",
        "whitelist": {"enabled": True, "tools": ["create"]},
    }
    tools, status = reg._discover_one("srv", server_cfg, timeout=5)
    assert [t["name"] for t in tools] == ["create"]
    assert status["status"] == "connected"
    assert status["tool_count"] == 1


def test_cache_invalidated_by_whitelist_change():
    """Ключ ToolsCache включает whitelist → изменение списка инвалидирует кэш."""
    import time as _time

    base = {"srv": {"transport": "stdio", "command": "x"}}
    with_wl = {
        "srv": {
            "transport": "stdio",
            "command": "x",
            "whitelist": {"enabled": True, "tools": ["a"]},
        }
    }
    cache = reg.ToolsCache({"mcp-servers": base})
    key_base = cache._make_cache_key(base)
    key_wl = cache._make_cache_key(with_wl)
    assert key_base != key_wl
    cache._data = {"mcp_srv_a": {}}
    cache._fetched_at = _time.time()
    cache._cache_key = key_base
    assert cache._is_valid(key_base, ttl=3600) is True
    assert cache._is_valid(key_wl, ttl=3600) is False


def test_apply_tool_blocks_tool_outside_tools_data(monkeypatch):
    """Enforcement: вызов не-whitelisted имени не доходит до MCP-транспорта."""
    from gc_core import tools as tools_pkg
    from gc_core.tools.connect_mcp import api as mcp_api

    calls = []
    monkeypatch.setattr(
        mcp_api, "call_mcp_tool", lambda *a, **kw: calls.append(a) or "never"
    )
    out = tools_pkg.apply_tool("mcp_srv_delete", "{}", tools_data_={})
    assert "mcp_srv_delete" in out
    assert calls == []
