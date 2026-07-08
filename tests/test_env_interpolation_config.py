import yaml

from config import load_config, apply_cli_overrides


def _write_cfg(tmp_path, body: dict) -> str:
    p = tmp_path / "config.yml"
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    return str(p)


def test_load_config_resolves_env_in_mcp_and_headers(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ght_xyz")
    monkeypatch.setenv("OR_KEY", "or_abc")
    cfg_path = _write_cfg(tmp_path, {
        "backend": {"api_mode": "chat_completions",
                    "chat_completion_headers": {"Authorization": "Bearer ${ENV:OR_KEY}"}},
        "mcp": {"servers": {"github": {"transport": "stdio", "command": "x",
                 "token": "${ENV:GITHUB_TOKEN}", "env": {"GITHUB_TOKEN": "${ENV:GITHUB_TOKEN}"}}}},
    })
    cfg = load_config(cfg_path)
    assert cfg["chat_completion_headers"]["Authorization"] == "Bearer or_abc"
    servers = cfg["mcp-servers"]
    assert servers["github"]["token"] == "ght_xyz"
    assert servers["github"]["env"]["GITHUB_TOKEN"] == "ght_xyz"


def test_cli_override_resolves_env(monkeypatch):
    monkeypatch.setenv("U", "http://h/v1/chat")
    cd = {"completions_url": "orig"}
    apply_cli_overrides(cd, extra_overrides={"completions_url": "${ENV:U}"})
    assert cd["completions_url"] == "http://h/v1/chat"
