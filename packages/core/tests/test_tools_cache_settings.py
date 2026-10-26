"""ToolsCache у встраивающего: настройки вместо словаря нижнего слоя."""

from gc_core.tools.connect_mcp.registry import ToolsCache
from gc_testkit.settings import make_settings


def test_tools_cache_takes_settings_and_projects_them_like_the_core():
    loaded = make_settings(
        {
            "mcp.servers": {
                "demo": {"transport": "sse", "url": "http://127.0.0.1:1/sse"}
            },
            "mcp.meta.discovery_timeout_sec": 5,
        }
    )
    cache = ToolsCache(loaded)
    assert set(cache._config["mcp-servers"]) == {"demo"}
    assert cache._discovery_timeout() == 5

    cache.rebind(make_settings())
    assert cache._config["mcp-servers"] == {}


def test_tools_cache_still_takes_the_lower_layer_dict():
    raw = {"mcp-servers": {}, "mcp-meta": {"discovery_timeout_sec": 3}}
    assert ToolsCache(raw)._discovery_timeout() == 3
