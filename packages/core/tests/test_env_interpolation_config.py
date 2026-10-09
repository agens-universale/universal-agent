import yaml
from gc_core.config import apply_cli_overrides, load_settings
from gc_core.config_modules.field_meta import plain_dump
from gc_testkit.settings import make_settings


def _write_cfg(tmp_path, body: dict) -> str:
    p = tmp_path / "config.yml"
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    return str(p)


def test_load_settings_resolves_env_in_mcp_and_headers(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ght_xyz")
    monkeypatch.setenv("OR_KEY", "or_abc")
    cfg_path = _write_cfg(
        tmp_path,
        {
            "backend": {
                "api_mode": "chat_completions",
                "chat_completion_headers": {
                    "Authorization": "Bearer ${ENV:OR_KEY}"
                },
            },
            "mcp": {
                "servers": {
                    "github": {
                        "transport": "stdio",
                        "command": "x",
                        "token": "${ENV:GITHUB_TOKEN}",
                        "env": {"GITHUB_TOKEN": "${ENV:GITHUB_TOKEN}"},
                    }
                }
            },
        },
    )
    settings = load_settings(cfg_path).settings
    headers = plain_dump(settings.backend)["chat_completion_headers"]
    assert headers["Authorization"] == "Bearer or_abc"
    github = plain_dump(settings.mcp.servers["github"], by_alias=True)
    assert github["token"] == "ght_xyz"
    assert github["env"]["GITHUB_TOKEN"] == "ght_xyz"


def test_cli_override_resolves_env(monkeypatch):
    monkeypatch.setenv("U", "http://h/v1/chat")
    loaded = make_settings({"backend.completions_url": "orig"})
    settings = apply_cli_overrides(
        loaded, extra_overrides={"backend.completions_url": "${ENV:U}"}
    ).settings
    assert settings.backend.completions_url == "http://h/v1/chat"


def test_cli_override_file_placeholder(tmp_path):
    f = tmp_path / "tok"
    f.write_text("cli-secret\n")
    settings = apply_cli_overrides(
        make_settings({}),
        extra_overrides={
            "backend.chat_completion_headers": {
                "Authorization": f"Bearer ${{FILE:{f}}}"
            }
        },
    ).settings
    headers = plain_dump(settings.backend)["chat_completion_headers"]
    assert headers["Authorization"] == "Bearer cli-secret"


def test_validate_config_text_resolves_env_like_load_settings(monkeypatch):
    """Writer валидирует конфиг тем же конвейером, что и загрузчик: ${ENV:} → compat → схема.

    load_settings резолвит ${ENV:VAR} до валидации, а validate_config_text — нет. В строковом
    поле это незаметно, но в нестроковом (backend.request_timeout_sec: int) плейсхолдер
    не проходит схему: агент на таком конфиге работает, а сохранение в /settings, финал
    онбординга и --sync-config падают «конфиг не проходит схему» — юзер заблокирован в UI
    без внятной причины.
    """
    from gc_core import config as config_mod

    monkeypatch.setenv("GC_BACKEND_TIMEOUT", "42")
    text = (
        "backend:\n"
        '  completions_url: "http://h/v1/chat/completions"\n'
        '  request_timeout_sec: "${ENV:GC_BACKEND_TIMEOUT}"\n'
    )
    config_mod.validate_config_text(
        text
    )  # не бросает: ${ENV:} резолвится как в load_settings


def test_validate_config_text_interpolates_before_compat(monkeypatch):
    """Порядок конвейера validate_config_text — ${ENV:VAR} ДО apply_compat (как в
    load_settings, который перенёс интерполяцию на per-file фазу до compat/валидации).
    Пин через spy на реальных функциях модуля — не зависит от текущего (пустого)
    состояния config_modules.compat.RENAMES."""
    # Порядок держит `validate_config_text`, а её дом — `config_modules.layers`
    # (агрегатор `config` только ре-экспортирует). Подменять надо там, где имена
    # резолвятся, иначе шпион до функции не доходит и `calls` остаётся пустым.
    from gc_core import config as config_mod
    from gc_core.config_modules import layers as layers_mod

    calls: list[str] = []
    orig_interp = layers_mod.resolve_env_placeholders
    orig_compat = layers_mod.apply_compat

    def spy_interp(obj):
        calls.append("interp")
        return orig_interp(obj)

    def spy_compat(raw):
        calls.append("compat")
        return orig_compat(raw)

    monkeypatch.setattr(layers_mod, "resolve_env_placeholders", spy_interp)
    monkeypatch.setattr(layers_mod, "apply_compat", spy_compat)

    config_mod.validate_config_text(
        'backend:\n  completions_url: "http://h/v1/chat/completions"\n'
    )
    assert calls == ["interp", "compat"]
