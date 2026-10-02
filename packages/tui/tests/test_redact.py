from gc_tui.services.redact import redact_secrets


def test_masks_secret_keys_deeply():
    cfg = {
        "model": "M",
        "chat_completions_auth_token": "sk-REAL",
        "backend": {"openrouter_api_key": "or-REAL", "use_ssl": True},
        "mcp-servers": {
            "s": {
                "token": "tok-REAL",
                "url": "http://x",
                "headers": {
                    "Authorization": "Bearer abc",
                    "X-API-Key": "key-123",
                },
            }
        },
        "nested": [{"password": "p"}, {"keep": "v"}],
    }
    out = redact_secrets(cfg)
    assert out["chat_completions_auth_token"] == "***"
    assert out["backend"]["openrouter_api_key"] == "***"
    assert out["mcp-servers"]["s"]["token"] == "***"
    assert out["nested"][0]["password"] == "***"
    # не-секреты целы; оригинал не мутирован
    assert out["model"] == "M" and out["backend"]["use_ssl"] is True
    assert out["mcp-servers"]["s"]["url"] == "http://x"
    assert out["mcp-servers"]["s"]["headers"]["Authorization"] == "***"
    assert out["mcp-servers"]["s"]["headers"]["X-API-Key"] == "***"
    assert cfg["chat_completions_auth_token"] == "sk-REAL"


def test_flat_view_secrets_masked_by_schema_metadata():
    """Плоский вид: секреты схемы маскируются по метаданным, даже когда имя
    ключа на секрет не похоже (proxy, X-Tenant, env)."""
    from gc_core.config import flatten_app_config
    from gc_core.config_modules.root import AppConfig

    settings = AppConfig.model_validate(
        {
            "backend": {
                "proxy": "http://u:pass-REAL@proxy:3128",
                "chat_completion_headers": {"X-Tenant": "tenant-REAL"},
            },
            "mcp": {
                "servers": {
                    "s": {
                        "transport": "stdio",
                        "command": "srv",
                        "env": {"GH": "env-REAL"},
                    }
                }
            },
            "agent_auth": {"auth_endpoint": "http://a", "login": "login-REAL"},
        }
    )
    flat = flatten_app_config(settings)
    out = redact_secrets(flat)
    assert out["proxy"] == "***"
    assert out["chat_completion_headers"] == {"X-Tenant": "***"}
    assert out["mcp-servers"]["s"]["env"] == {"GH": "***"}
    assert out["mcp-servers"]["s"]["command"] == "srv"
    assert out["agent_auth"] == {"auth_endpoint": "***", "login": "***"}
    assert "REAL" not in repr(out)
    # оригинал не мутирован
    assert flat["proxy"] == "http://u:pass-REAL@proxy:3128"


def test_redact_settings_uses_schema_metadata():
    from gc_core.config_modules.root import AppConfig
    from gc_tui.services.redact import redact_settings

    settings = AppConfig.model_validate(
        {
            "voice": {"url": "http://stt", "token": "voice-REAL"},
            "models": {"m": {"url": "http://m", "token": "model-REAL"}},
        }
    )
    out = redact_settings(settings)
    assert out["voice"] == {
        **out["voice"],
        "url": "http://stt",
        "token": "***",
    }
    assert out["models"]["m"]["token"] == "***"
    assert "REAL" not in repr(out)
