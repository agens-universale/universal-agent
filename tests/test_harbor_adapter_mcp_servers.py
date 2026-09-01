import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "harbor"))

from adapters.gc_cli_external import GcCliExternal  # noqa: E402


def _server(name, transport, url=None, command=None, args=None):
    return SimpleNamespace(
        name=name, transport=transport, url=url, command=command, args=args or []
    )


def test_streamable_http_maps_to_stream_http_on_os_backend():
    mapped = GcCliExternal._harbor_mcp_to_config(
        [_server("user-server", "streamable-http", url="http://user-server:8000/mcp")]
    )
    assert mapped["user_server"]["transport"] == "stream_http"
    assert mapped["user_server"]["url"] == "http://user-server:8000/mcp"
    assert mapped["user_server"]["execute_on"] == "os_backend"


def test_transport_is_never_substituted():
    mapped = GcCliExternal._harbor_mcp_to_config([
        _server("a", "sse", url="http://a:1/mcp"),
        _server("b", "stdio", command="srv", args=["--x"]),
    ])
    assert mapped["a"]["transport"] == "sse"
    assert mapped["b"]["transport"] == "stdio"
    assert mapped["b"]["command"] == "srv"
    assert mapped["b"]["args"] == ["--x"]


def test_name_is_sanitized_for_the_tool_prefix():
    mapped = GcCliExternal._harbor_mcp_to_config(
        [_server("user server.v2", "streamable-http", url="http://x/mcp")]
    )
    assert "user_server_v2" in mapped


def test_unknown_transport_is_skipped():
    mapped = GcCliExternal._harbor_mcp_to_config(
        [_server("weird", "carrier-pigeon", url="http://x/mcp")]
    )
    assert mapped == {}


def test_existing_config_entry_wins_over_autotagging():
    config_dict = {
        "mcp-servers": {
            "user_server": {
                "transport": "stream_http",
                "url": "http://user-server:8000/mcp",
                "execute_on": "agent",
            }
        }
    }
    GcCliExternal._apply_mcp_servers(
        config_dict,
        [_server("user-server", "streamable-http", url="http://user-server:8000/mcp")],
    )
    assert config_dict["mcp-servers"]["user_server"]["execute_on"] == "agent"


def test_empty_server_list_leaves_config_untouched():
    config_dict = {"mcp-servers": {"keep": {"transport": "http", "url": "http://k"}}}
    GcCliExternal._apply_mcp_servers(config_dict, [])
    assert config_dict["mcp-servers"] == {
        "keep": {"transport": "http", "url": "http://k"}
    }
    assert "mcp-meta" not in config_dict


def test_timeouts_are_raised_for_exec_round_trips():
    config_dict = {}
    GcCliExternal._apply_mcp_servers(
        config_dict,
        [_server("user-server", "streamable-http", url="http://user-server:8000/mcp")],
    )
    assert config_dict["mcp-meta"]["request_timeout_sec"] >= 600
    assert config_dict["mcp-meta"]["discovery_timeout_sec"] >= 120


def test_existing_higher_timeouts_are_kept():
    config_dict = {"mcp-meta": {"request_timeout_sec": 900}}
    GcCliExternal._apply_mcp_servers(
        config_dict, [_server("s", "streamable-http", url="http://x/mcp")]
    )
    assert config_dict["mcp-meta"]["request_timeout_sec"] == 900
