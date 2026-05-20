from science_mcp.config import load_config


def test_defaults(monkeypatch):
    for k in [
        "SCIENCE_MCP_MAILTO", "NCBI_API_KEY", "SEMANTIC_SCHOLAR_API_KEY",
        "CORE_API_KEY", "SCIENCE_MCP_CACHE_DIR", "SCIENCE_MCP_CACHE",
        "SCIENCE_MCP_HTTP_TIMEOUT", "SCIENCE_MCP_QUEUE_TIMEOUT",
    ]:
        monkeypatch.delenv(k, raising=False)
    cfg = load_config()
    assert cfg.mailto == "team@company.example"
    assert cfg.ncbi_api_key is None
    assert cfg.cache_enabled is True
    assert cfg.http_timeout_sec == 15
    assert cfg.queue_timeout_sec == 120


def test_overrides(monkeypatch):
    monkeypatch.setenv("SCIENCE_MCP_MAILTO", "real@team.io")
    monkeypatch.setenv("NCBI_API_KEY", "abc")
    monkeypatch.setenv("SCIENCE_MCP_CACHE", "0")
    monkeypatch.setenv("SCIENCE_MCP_HTTP_TIMEOUT", "30")
    cfg = load_config()
    assert cfg.mailto == "real@team.io"
    assert cfg.ncbi_api_key == "abc"
    assert cfg.cache_enabled is False
    assert cfg.http_timeout_sec == 30


def test_keys_configured(monkeypatch):
    monkeypatch.delenv("CORE_API_KEY", raising=False)
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.setenv("NCBI_API_KEY", "x")
    cfg = load_config()
    assert cfg.keys_configured() == {
        "ncbi": True,
        "semantic_scholar": False,
        "core": False,
    }


def test_mailto_configured_flag(monkeypatch):
    monkeypatch.setenv("SCIENCE_MCP_MAILTO", "team@company.example")
    assert load_config().mailto_is_real() is False
    monkeypatch.setenv("SCIENCE_MCP_MAILTO", "real@org.io")
    assert load_config().mailto_is_real() is True


def test_host_port_defaults(monkeypatch):
    monkeypatch.delenv("SCIENCE_MCP_HOST", raising=False)
    monkeypatch.delenv("SCIENCE_MCP_PORT", raising=False)
    cfg = load_config()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8765


def test_host_port_overrides(monkeypatch):
    monkeypatch.setenv("SCIENCE_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("SCIENCE_MCP_PORT", "9000")
    cfg = load_config()
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9000
