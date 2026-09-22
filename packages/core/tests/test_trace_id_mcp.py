from gc_core.tools.connect_mcp.agent_tools import _servers_cfg_with_trace


def _servers():
    """Рассылка trace — opt-in: без `trace_header_enabled: true` сервер не получает ничего."""
    return {
        "http_srv": {"transport": "http", "url": "http://a/mcp", "trace_header_enabled": True},
        "sse_srv": {
            "transport": "sse",
            "url": "http://b/mcp",
            "headers": {"X-Keep": "1"},
            "trace_header_enabled": True,
        },
        "shttp_srv": {"transport": "stream_http", "url": "http://c/mcp", "trace_header_enabled": True},
        "ssse_srv": {"transport": "stream_sse", "url": "http://d/mcp", "trace_header_enabled": True},
        "stdio_srv": {
            "transport": "stdio",
            "command": "srv",
            "env": {"KEEP": "1"},
            "trace_header_enabled": True,
        },
        "sstdio_srv": {"transport": "stream_stdio", "command": "srv", "trace_header_enabled": True},
        "opted_out": {"transport": "http", "url": "http://e/mcp", "trace_header_enabled": False},
        "no_flag": {"transport": "http", "url": "http://f/mcp"},
    }


class TestAbsentTrace:
    def test_returns_same_object(self):
        servers = _servers()
        assert _servers_cfg_with_trace(servers, {}) is servers

    def test_empty_string_returns_same_object(self):
        servers = _servers()
        assert _servers_cfg_with_trace(servers, {"trace_id": "  "}) is servers


class TestPresentTrace:
    def test_http_family_gets_header(self):
        out = _servers_cfg_with_trace(_servers(), {"trace_id": "wf-42"})
        for name in ("http_srv", "sse_srv", "shttp_srv", "ssse_srv"):
            assert out[name]["headers"]["trace_id"] == "wf-42", name

    def test_existing_headers_are_preserved(self):
        out = _servers_cfg_with_trace(_servers(), {"trace_id": "wf-42"})
        assert out["sse_srv"]["headers"]["X-Keep"] == "1"

    def test_stdio_family_gets_env_and_no_header(self):
        out = _servers_cfg_with_trace(_servers(), {"trace_id": "wf-42"})
        for name in ("stdio_srv", "sstdio_srv"):
            assert out[name]["env"]["TRACE_ID"] == "wf-42", name
            assert "trace_id" not in (out[name].get("headers") or {}), name
        assert out["stdio_srv"]["env"]["KEEP"] == "1"

    def test_server_gets_nothing_without_explicit_opt_in(self):
        """Дефолт — не отправлять: и явный `trace_header_enabled: false`, и отсутствие ключа."""
        out = _servers_cfg_with_trace(_servers(), {"trace_id": "wf-42"})
        for name in ("opted_out", "no_flag"):
            assert "trace_id" not in (out[name].get("headers") or {}), name
            assert "TRACE_ID" not in (out[name].get("env") or {}), name

    def test_custom_header_name_is_used(self):
        out = _servers_cfg_with_trace(_servers(), {"trace_id": "wf-42", "trace_id_header_name": "X-Trace-Id"})
        assert out["http_srv"]["headers"]["X-Trace-Id"] == "wf-42"
        assert "trace_id" not in out["http_srv"]["headers"]

    def test_original_dicts_are_not_mutated(self):
        servers = _servers()
        out = _servers_cfg_with_trace(servers, {"trace_id": "wf-42"})
        # Мутация запрещена: в серверном режиме этот словарь общий с
        # процесс-глобальным конфигом, а раны идут в конкурентных потоках.
        assert servers["http_srv"].get("headers") in (None, {})
        assert servers["sse_srv"]["headers"] == {"X-Keep": "1"}
        assert servers["stdio_srv"]["env"] == {"KEEP": "1"}
        assert out is not servers
        assert out["http_srv"] is not servers["http_srv"]
