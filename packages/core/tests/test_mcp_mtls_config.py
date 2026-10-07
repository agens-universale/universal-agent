"""mTLS-поля MCP-сервера (verify-ssl / client-cert / client-key) проходят схему
и доходят до рантайма ssl_config — раньше extra=forbid резал их на загрузке."""

from gc_core.config_modules.mcp import McpServerConfig
from gc_core.utils.ssl_config import get_client_cert, get_verify_ssl


def test_mcp_server_config_accepts_mtls_fields():
    srv = McpServerConfig(
        **{
            "transport": "stream_http",
            "url": "https://mcp.example/mcp",
            "server-name": "sbertrack",
            "verify-ssl": False,
            "client-cert": "C:/Work/certificates/tcerts/os_cert.pem",
            "client-key": "C:/Work/certificates/tcerts/os_key.pem",
        }
    )
    dumped = srv.model_dump(by_alias=True)
    # ключи сохраняются под алиасами (как читает рантайм через config_dict["mcp-servers"])
    assert dumped["verify-ssl"] is False
    assert dumped["client-cert"] == "C:/Work/certificates/tcerts/os_cert.pem"
    assert dumped["client-key"] == "C:/Work/certificates/tcerts/os_key.pem"
    # рантайм читает их как и сейчас
    assert get_verify_ssl(dumped) is False
    assert get_client_cert(dumped) == (
        "C:/Work/certificates/tcerts/os_cert.pem",
        "C:/Work/certificates/tcerts/os_key.pem",
    )


def test_mcp_server_config_defaults_no_mtls():
    srv = McpServerConfig(transport="stream_http", url="https://x/mcp")
    dumped = srv.model_dump(by_alias=True)
    # без сертов — проверка SSL включена, клиентского серта нет (поведение не меняется)
    assert get_verify_ssl(dumped) is True
    assert get_client_cert(dumped) is None


def test_mcp_server_config_strips_cert_paths():
    srv = McpServerConfig(
        **{
            "transport": "stream_http",
            "url": "https://x/mcp",
            "client-cert": "  C:/c.pem  ",
            "client-key": "  C:/k.pem  ",
        }
    )
    dumped = srv.model_dump(by_alias=True)
    assert dumped["client-cert"] == "C:/c.pem"
    assert dumped["client-key"] == "C:/k.pem"
