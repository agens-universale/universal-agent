from pathlib import Path

import pytest

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


# YAML config support — primary source. Env is fallback for fields the YAML
# does not set. Default search paths (./science-mcp.yaml, ~/.config/...) are
# checked only when no explicit path is provided.

def _clear_env(monkeypatch):
    for k in [
        "SCIENCE_MCP_MAILTO", "NCBI_API_KEY", "SEMANTIC_SCHOLAR_API_KEY",
        "CORE_API_KEY", "SCIENCE_MCP_CACHE_DIR", "SCIENCE_MCP_CACHE",
        "SCIENCE_MCP_HTTP_TIMEOUT", "SCIENCE_MCP_QUEUE_TIMEOUT",
        "SCIENCE_MCP_HOST", "SCIENCE_MCP_PORT",
    ]:
        monkeypatch.delenv(k, raising=False)


def test_yaml_full_load(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    cfg_file = tmp_path / "science-mcp.yaml"
    cfg_file.write_text(
        "mailto: real@team.io\n"
        "host: 0.0.0.0\n"
        "port: 9000\n"
        "ncbi_api_key: abc\n"
        "semantic_scholar_api_key: def\n"
        "core_api_key: ghi\n"
        f"cache_dir: {tmp_path / 'cache'}\n"
        "cache_enabled: false\n"
        "http_timeout_sec: 30\n"
        "queue_timeout_sec: 60\n"
    )
    cfg = load_config(yaml_path=cfg_file)
    assert cfg.mailto == "real@team.io"
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9000
    assert cfg.ncbi_api_key == "abc"
    assert cfg.semantic_scholar_api_key == "def"
    assert cfg.core_api_key == "ghi"
    assert cfg.cache_dir == tmp_path / "cache"
    assert cfg.cache_enabled is False
    assert cfg.http_timeout_sec == 30
    assert cfg.queue_timeout_sec == 60


def test_yaml_partial_with_env_fallback(tmp_path, monkeypatch):
    """YAML covers some fields, env fills the rest. YAML wins on overlap."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("NCBI_API_KEY", "from_env")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "from_env_too")
    monkeypatch.setenv("SCIENCE_MCP_HTTP_TIMEOUT", "99")
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text(
        "mailto: yaml@team.io\n"
        "ncbi_api_key: from_yaml\n"
    )
    cfg = load_config(yaml_path=cfg_file)
    assert cfg.mailto == "yaml@team.io"
    assert cfg.ncbi_api_key == "from_yaml"            # yaml wins
    assert cfg.semantic_scholar_api_key == "from_env_too"  # env fallback
    assert cfg.http_timeout_sec == 99                 # env fallback
    assert cfg.port == 8765                           # neither -> default


def test_yaml_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(yaml_path=tmp_path / "does-not-exist.yaml")


def test_yaml_default_search_cwd(tmp_path, monkeypatch):
    """No explicit path: ./science-mcp.yaml in cwd is picked up."""
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "science-mcp.yaml").write_text("mailto: from_cwd@team.io\n")
    cfg = load_config()
    assert cfg.mailto == "from_cwd@team.io"


def test_yaml_empty_file_falls_back_to_env_and_defaults(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SCIENCE_MCP_MAILTO", "env@team.io")
    cfg_file = tmp_path / "empty.yaml"
    cfg_file.write_text("")
    cfg = load_config(yaml_path=cfg_file)
    assert cfg.mailto == "env@team.io"
    assert cfg.port == 8765


def test_yaml_rejects_unknown_keys(tmp_path, monkeypatch):
    """Typo guard — unknown top-level keys explode, like AppConfig in gc-cli."""
    _clear_env(monkeypatch)
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("mailtto: typo@x.io\n")  # double-t typo
    with pytest.raises(ValueError, match="unknown"):
        load_config(yaml_path=cfg_file)
