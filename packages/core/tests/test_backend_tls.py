def test_backend_config_tls_fields():
    from gc_core.config_modules.backend import BackendConfig

    c = BackendConfig(use_ssl="/ca.pem", cert_path="/c.crt", key_path="/k.key")
    assert c.use_ssl == "/ca.pem"
    assert c.cert_path == "/c.crt"
    assert c.key_path == "/k.key"
    # backward compat: bool use_ssl + optional cert
    assert BackendConfig().use_ssl is True
    assert BackendConfig().cert_path is None
    assert BackendConfig(use_ssl=False).use_ssl is False


def test_settings_map_backend_tls(tmp_path):
    import yaml
    from gc_core.config import load_settings

    p = tmp_path / "config.yml"
    p.write_text(
        yaml.safe_dump(
            {
                "backend": {
                    "api_mode": "chat_completions",
                    "use_ssl": "/ca.pem",
                    "cert_path": "/c.crt",
                    "key_path": "/k.key",
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = load_settings(str(p)).settings
    assert cfg.backend.use_ssl == "/ca.pem"
    assert cfg.backend.cert_path == "/c.crt"
    assert cfg.backend.key_path == "/k.key"


def test_backend_config_default_use_ssl_bool_maps(tmp_path):
    import yaml
    from gc_core.config import load_settings

    p = tmp_path / "config.yml"
    p.write_text(
        yaml.safe_dump({"backend": {"api_mode": "chat_completions"}}),
        encoding="utf-8",
    )
    cfg = load_settings(str(p)).settings
    assert cfg.backend.use_ssl is True
    assert cfg.backend.cert_path is None
    assert cfg.backend.key_path is None


def test_get_backend_client_cert():
    from gc_core.utils.ssl_config import get_backend_client_cert

    assert get_backend_client_cert({"cert_path": "/c", "key_path": "/k"}) == (
        "/c",
        "/k",
    )
    assert (
        get_backend_client_cert({"cert_path": "/c"}) is None
    )  # only one → None
    assert get_backend_client_cert({}) is None


from gc_testkit import run_coro
from transport_fakes import install_transport, sse_body, stream_response


def test_chat_completions_passes_verify_and_cert(monkeypatch):
    from gc_core.utils import connect_backend

    spy = install_transport(monkeypatch)
    if True:
        run_coro(
            connect_backend.arequest_backend_chat_completions(
                {"messages": []},
                {},
                url="https://b/v1/chat",
                verify="/ca.pem",
                cert=("/c.crt", "/k.key"),
                use_retry=False,
            )
        )
    kwargs = spy.last_client_kwargs
    assert kwargs["verify"] == "/ca.pem"
    assert kwargs["cert"] == ("/c.crt", "/k.key")


def test_responses_passes_verify_and_cert(monkeypatch):
    """Режим responses (deprecated) — тот же контракт TLS, что у chat/completions."""
    from gc_core.utils import connect_backend

    spy = install_transport(
        monkeypatch,
        stream_response(
            sse_body(
                {
                    "type": "response.completed",
                    "response": {"id": "r1", "output": []},
                },
                done=False,
            )
        ),
    )
    run_coro(
        connect_backend.arequest_backend_responses(
            {"messages": []},
            {},
            url="https://b/v1/responses",
            verify="/ca.pem",
            cert=("/c.crt", "/k.key"),
            use_retry=False,
        )
    )

    kwargs = spy.last_client_kwargs
    assert kwargs["verify"] == "/ca.pem"
    assert kwargs["cert"] == ("/c.crt", "/k.key")


def test_backend_cert_defaults_none_backward_compat(monkeypatch):
    from gc_core.utils import connect_backend

    spy = install_transport(monkeypatch)
    if True:
        run_coro(
            connect_backend.arequest_backend_chat_completions(
                {"messages": []},
                {},
                url="https://b/v1/chat",
                verify=True,
                use_retry=False,
            )
        )
    assert spy.last_client_kwargs["cert"] is None
    assert spy.last_client_kwargs["verify"] is True


def test_mcp_use_ssl_alias_unified():
    """MCP verify_ssl принимает унифицированные ключи use-ssl/use_ssl (как backend),
    а также verify-ssl (обратная совместимость); в дамп модели идёт verify-ssl."""
    from gc_core.config_modules.mcp import McpServerConfig

    for key in ("use-ssl", "use_ssl", "verify-ssl", "verify_ssl"):
        c = McpServerConfig(
            **{"transport": "http", "url": "https://x", key: "/ca.pem"}
        )
        assert c.verify_ssl == "/ca.pem", key
    assert (
        McpServerConfig(transport="http", url="https://x").verify_ssl is True
    )
    dumped = McpServerConfig(
        **{"transport": "http", "url": "https://x", "use-ssl": False}
    ).model_dump(by_alias=True)
    assert (
        dumped["verify-ssl"] is False
    )  # get_verify_ssl читает ключ verify-ssl
