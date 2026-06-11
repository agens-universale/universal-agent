from tui.services.redact import redact_secrets


def test_masks_secret_keys_deeply():
    cfg = {
        "model": "M",
        "chat_completions_auth_token": "sk-REAL",
        "backend": {"openrouter_api_key": "or-REAL", "use_ssl": True},
        "mcp-servers": {"s": {"token": "tok-REAL", "url": "http://x"}},
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
    assert cfg["chat_completions_auth_token"] == "sk-REAL"
