import pytest
from gc_core.tools.connect_mcp.client_port import AgentRuntimeClient
from gc_core.tools.connect_mcp.execute_on import McpExecuteOnError, resolve_mcp_client


class _LocalBackend:
    is_sandbox = False


class _SandboxBackend:
    is_sandbox = True


def test_no_backend_resolves_to_agent():
    client = resolve_mcp_client("srv", {"transport": "stream_http", "url": "http://x"})
    assert isinstance(client, AgentRuntimeClient)


def test_default_locus_is_agent_even_on_sandbox():
    client = resolve_mcp_client(
        "srv",
        {"transport": "stream_http", "url": "http://x"},
        os_backend=_SandboxBackend(),
    )
    assert isinstance(client, AgentRuntimeClient)


def test_local_backend_ignores_os_backend_locus():
    client = resolve_mcp_client(
        "srv",
        {"transport": "stream_http", "url": "http://x", "execute_on": "os_backend"},
        os_backend=_LocalBackend(),
    )
    assert isinstance(client, AgentRuntimeClient)


@pytest.mark.parametrize("transport", ["http", "stream_http", "stdio"])
def test_sandbox_plus_os_backend_resolves_to_stateless(transport):
    from gc_core.tools.connect_mcp.os_backend_client import OsBackendStatelessClient

    cfg = {"transport": transport, "url": "http://x", "command": "python3",
           "execute_on": "os_backend"}
    client = resolve_mcp_client("srv", cfg, os_backend=_SandboxBackend())
    assert isinstance(client, OsBackendStatelessClient)


@pytest.mark.parametrize("transport", ["sse", "stream_sse", "stream_stdio"])
def test_session_bound_transports_are_a_resolve_error(transport):
    cfg = {"transport": transport, "url": "http://x", "command": "python3",
           "execute_on": "os_backend"}
    with pytest.raises(McpExecuteOnError) as excinfo:
        resolve_mcp_client("srv", cfg, os_backend=_SandboxBackend())
    assert transport in str(excinfo.value)
    assert "srv" in str(excinfo.value)


def test_unknown_locus_is_an_error():
    cfg = {"transport": "stream_http", "url": "http://x", "execute_on": "moon"}
    with pytest.raises(McpExecuteOnError):
        resolve_mcp_client("srv", cfg, os_backend=_SandboxBackend())


def test_client_cert_on_os_backend_is_a_config_error():
    """TLS material would need a file in the container, and we copy none."""
    cfg = {"transport": "stream_http", "url": "https://x", "execute_on": "os_backend",
           "client-cert": "/etc/ssl/client.pem"}
    with pytest.raises(McpExecuteOnError) as excinfo:
        resolve_mcp_client("srv", cfg, os_backend=_SandboxBackend())
    assert "client-cert" in str(excinfo.value)


def test_ca_path_in_verify_ssl_on_os_backend_is_a_config_error():
    cfg = {"transport": "stream_http", "url": "https://x", "execute_on": "os_backend",
           "verify-ssl": "/etc/ssl/ca.pem"}
    with pytest.raises(McpExecuteOnError):
        resolve_mcp_client("srv", cfg, os_backend=_SandboxBackend())


def test_verify_ssl_false_on_os_backend_is_allowed():
    """A bare flag needs no file, so it survives the exec hop."""
    from gc_core.tools.connect_mcp.os_backend_client import OsBackendStatelessClient

    cfg = {"transport": "stream_http", "url": "https://x", "execute_on": "os_backend",
           "verify-ssl": False}
    client = resolve_mcp_client("srv", cfg, os_backend=_SandboxBackend())
    assert isinstance(client, OsBackendStatelessClient)


def test_tls_material_is_fine_on_the_agent_locus():
    cfg = {"transport": "stream_http", "url": "https://x",
           "client-cert": "/etc/ssl/client.pem"}
    assert isinstance(
        resolve_mcp_client("srv", cfg, os_backend=_SandboxBackend()),
        AgentRuntimeClient,
    )
