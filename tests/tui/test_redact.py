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
                "headers": {"Authorization": "Bearer abc", "X-API-Key": "key-123"},
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
