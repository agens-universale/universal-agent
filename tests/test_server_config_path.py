import pytest

from config_modules import env_vars
from config_modules.env_vars import EnvVars
from rest_api.server_config import load_server_config


def test_server_config_path_from_env_vars(monkeypatch, tmp_path):
    custom = tmp_path / "my_server.yml"
    custom.write_text(
        "server:\n  host: 0.0.0.0\n  port: 9999\nagent:\n  server_agentmd_path: a.md\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(env_vars, "env", EnvVars(server_config_path=custom))
    cfg = load_server_config()
    assert cfg["server"].get("port") == 9999


def test_server_config_path_none_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.setattr(env_vars, "env", EnvVars(server_config_path=None))
    monkeypatch.setattr("rest_api.server_config.PROJECT_ROOT", str(tmp_path))
    cfg = load_server_config()
    assert cfg["server"]["host"] == "0.0.0.0"
    assert cfg["server"]["port"] == 8000
