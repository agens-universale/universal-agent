import sys
from types import SimpleNamespace

# Пришло из develop, где тест лежал в корневом tests/ и якорем работал
# `parent.parent / "harbor"`. У нас тест живёт в packages/core/tests, а стенд —
# в integrations/harbor: тот же якорь указывал бы в packages/core/harbor.
# Спрашиваем раскладку у gc_testkit.paths, как соседние harbor-тесты.
from gc_testkit.paths import HARBOR

if str(HARBOR) not in sys.path:
    sys.path.insert(0, str(HARBOR))

from adapters.gc_cli_external import GcCliExternal


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


def test_sse_is_not_tagged_os_backend():
    """sse needs a live session; tagging it os_backend builds a config the
    resolver is guaranteed to reject."""
    mapped = GcCliExternal._harbor_mcp_to_config(
        [_server("s", "sse", url="http://s:1/mcp")]
    )
    assert mapped["s"]["transport"] == "sse"
    assert mapped["s"]["execute_on"] == "agent"


def test_adapter_uses_the_resolvers_rule_not_a_copy():
    """One source of truth for which transports survive a stateless exec."""
    import adapters.gc_cli_external as mod
    from gc_core.tools.connect_mcp.execute_on import OS_BACKEND_TRANSPORTS

    assert not hasattr(mod, "_OS_BACKEND_CAPABLE")
    mapped = GcCliExternal._harbor_mcp_to_config([
        _server("h", "streamable-http", url="http://h:1/mcp"),
        _server("s", "sse", url="http://s:1/mcp"),
        _server("d", "stdio", command="srv"),
    ])
    for entry in mapped.values():
        expected = "os_backend" if entry["transport"] in OS_BACKEND_TRANSPORTS else "agent"
        assert entry["execute_on"] == expected


def test_stream_http_and_stdio_are_tagged_os_backend():
    mapped = GcCliExternal._harbor_mcp_to_config([
        _server("h", "streamable-http", url="http://h:1/mcp"),
        _server("d", "stdio", command="srv"),
    ])
    assert mapped["h"]["execute_on"] == "os_backend"
    assert mapped["d"]["execute_on"] == "os_backend"


def test_explicit_entry_wins_even_under_the_servers_natural_name():
    """The mapper sanitises `user-server` to `user_server`; comparing raw keys
    would register the same sidecar twice under two tool prefixes."""
    config_dict = {
        "mcp-servers": {
            "user-server": {
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
    assert list(config_dict["mcp-servers"]) == ["user-server"]
    assert config_dict["mcp-servers"]["user-server"]["execute_on"] == "agent"


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
