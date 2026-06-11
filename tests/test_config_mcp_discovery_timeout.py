from config_modules.mcp import McpMetaConfig


def test_discovery_timeout_default_is_30():
    assert McpMetaConfig().discovery_timeout_sec == 30


def test_discovery_timeout_from_value():
    assert McpMetaConfig(discovery_timeout_sec=5).discovery_timeout_sec == 5


def test_discovery_timeout_must_be_positive():
    import pytest
    with pytest.raises(Exception):
        McpMetaConfig(discovery_timeout_sec=0)
