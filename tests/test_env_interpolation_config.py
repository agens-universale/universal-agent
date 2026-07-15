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


def test_validate_config_text_resolves_env_like_load_config(monkeypatch):
    """Writer валидирует конфиг тем же конвейером, что и загрузчик: compat → ${ENV:} → схема.

    load_config резолвит ${ENV:VAR} до валидации, а validate_config_text — нет. В строковом
    поле это незаметно, но в нестроковом (backend.request_timeout_sec: int) плейсхолдер
    не проходит схему: агент на таком конфиге работает, а сохранение в /settings, финал
    онбординга и --sync-config падают «конфиг не проходит схему» — юзер заблокирован в UI
    без внятной причины.
    """
    import config as config_mod

    monkeypatch.setenv("GC_BACKEND_TIMEOUT", "42")
    text = (
        "backend:\n"
        '  completions_url: "http://h/v1/chat/completions"\n'
        '  request_timeout_sec: "${ENV:GC_BACKEND_TIMEOUT}"\n'
    )
    config_mod.validate_config_text(text)      # не бросает: ${ENV:} резолвится как в load_config
