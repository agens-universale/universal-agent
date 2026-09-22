import pytest
from gc_core.config_modules.mcp import McpServerConfig
from pydantic import ValidationError


def test_execute_on_defaults_to_agent():
    cfg = McpServerConfig(transport="stream_http", url="http://x:8000/mcp")
    assert cfg.execute_on == "agent"


def test_execute_on_accepts_os_backend():
    cfg = McpServerConfig(
        transport="stream_http", url="http://x:8000/mcp", execute_on="os_backend"
    )
    assert cfg.execute_on == "os_backend"


def test_execute_on_rejects_unknown_value():
    with pytest.raises(ValidationError):
        McpServerConfig(
            transport="stream_http", url="http://x:8000/mcp", execute_on="container"
        )


def test_execute_on_survives_model_dump_by_alias():
    cfg = McpServerConfig(
        transport="stdio", command="python3", execute_on="os_backend"
    )
    assert cfg.model_dump(by_alias=True)["execute_on"] == "os_backend"
