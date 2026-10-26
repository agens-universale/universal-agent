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


def test_raw_dicts_are_masked_by_key_name_only():
    """Сырой словарь — без схемы: маскировка только по имени ключа. Плоских имён
    прежнего config_dict больше нет, и имя, не похожее на секрет (proxy,
    X-Tenant, env), сырой словарь не прячет — секреты схемы маскирует
    ``redact_settings`` по метаданным."""
    raw = {
        "proxy": "http://u:pass@proxy:3128",
        "chat_completion_headers": {
            "X-Tenant": "tenant",
            "Authorization": "Bearer REAL",
        },
        "agent_auth": {"auth_endpoint": "http://a", "password": "pw-REAL"},
    }
    out = redact_secrets(raw)
    assert out["proxy"] == raw["proxy"]
    assert out["chat_completion_headers"] == {
        "X-Tenant": "tenant",
        "Authorization": "***",
    }
    assert out["agent_auth"] == {
        "auth_endpoint": "http://a",
        "password": "***",
    }
    assert "REAL" not in repr(out)
    # оригинал не мутирован
    assert raw["agent_auth"]["password"] == "pw-REAL"


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
