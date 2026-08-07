from config_modules.endpoint_presets import (
    DEFAULT_ENDPOINT_PRESETS,
    EndpointPreset,
    endpoint_presets_from_config,
)
from config_modules.root import AppConfig


def test_default_presets_names_and_urls():
    assert [p.name for p in DEFAULT_ENDPOINT_PRESETS] == ["OpenRouter", "Cloud.ru", "GigaCode"]
    assert DEFAULT_ENDPOINT_PRESETS[0].url == "https://openrouter.ai/api/v1/chat/completions"
    assert DEFAULT_ENDPOINT_PRESETS[1].url == "https://foundation-models.api.cloud.ru/v1/chat/completions"
    assert DEFAULT_ENDPOINT_PRESETS[2].url == "https://litellm-proxy.sberai-rnd.ru/v1/chat/completions"


def test_appconfig_defaults_to_presets():
    cfg = AppConfig()
    assert [p.name for p in cfg.endpoint_presets] == ["OpenRouter", "Cloud.ru", "GigaCode"]


def test_from_config_missing_key_gives_default():
    assert endpoint_presets_from_config({}) == list(DEFAULT_ENDPOINT_PRESETS)


def test_from_config_reads_user_list():
    cfg = {"endpoint_presets": [{"name": "My", "url": "http://x/v1/chat/completions"}]}
    out = endpoint_presets_from_config(cfg)
    assert out == [EndpointPreset(name="My", url="http://x/v1/chat/completions")]


def test_from_config_invalid_entries_fall_back_to_default():
    # сырой dict из TUI-тестов/легаси может быть не валидирован схемой
    assert endpoint_presets_from_config({"endpoint_presets": "garbage"}) == list(DEFAULT_ENDPOINT_PRESETS)


def test_load_config_flattens_custom_presets_into_config_dict(tmp_path, monkeypatch):
    """Регрессия (fix Important-1): flatten_app_config не пробрасывал
    endpoint_presets — TUI видел только кодовый дефолт, пользовательские
    пресеты из config.yml были мертвы."""
    import config as cfgmod
    import mcp_runtime.paths as mpaths

    monkeypatch.setattr(cfgmod, "_project_local_config_path", lambda: None)
    monkeypatch.setattr(mpaths, "managed_config_path", lambda: tmp_path / "none.yml")

    conf = tmp_path / "config.yml"
    conf.write_text(
        "endpoint_presets:\n  - name: My\n    url: http://x/v1/chat/completions\n",
        encoding="utf-8",
    )
    config_dict = cfgmod.load_config(str(conf))

    assert config_dict["endpoint_presets"] == [
        {"name": "My", "url": "http://x/v1/chat/completions"}
    ]
    assert endpoint_presets_from_config(config_dict) == [
        EndpointPreset(name="My", url="http://x/v1/chat/completions")
    ]
