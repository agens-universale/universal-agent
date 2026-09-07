"""voice_endpoint_presets: дефолты и чтение из сырого config-dict (спека §2)."""
from config_modules.endpoint_presets import EndpointPreset
from config_modules.root import AppConfig
from config_modules.voice_endpoint_presets import (
    DEFAULT_VOICE_ENDPOINT_PRESETS,
    default_voice_endpoint_presets,
    voice_endpoint_presets_from_config,
)


def test_defaults_are_openai_groq_mistral_in_owner_order():
    assert [(p.name, p.url) for p in DEFAULT_VOICE_ENDPOINT_PRESETS] == [
        ("OpenAI", "https://api.openai.com/v1/audio/transcriptions"),
        ("Groq", "https://api.groq.com/openai/v1/audio/transcriptions"),
        ("Mistral", "https://api.mistral.ai/v1/audio/transcriptions"),
    ]


def test_missing_key_falls_back_to_defaults():
    assert voice_endpoint_presets_from_config({}) == default_voice_endpoint_presets()
    assert voice_endpoint_presets_from_config(None) == default_voice_endpoint_presets()


def test_config_key_overrides_defaults():
    cfg = {"voice_endpoint_presets": [{"name": "Свой", "url": "https://x/v1/audio/transcriptions"}]}
    out = voice_endpoint_presets_from_config(cfg)
    assert [(p.name, p.url) for p in out] == [("Свой", "https://x/v1/audio/transcriptions")]


def test_malformed_item_falls_back_to_defaults_entirely():
    cfg = {"voice_endpoint_presets": [{"name": "без url"}]}
    assert voice_endpoint_presets_from_config(cfg) == default_voice_endpoint_presets()


def test_defaults_are_a_fresh_list_each_call():
    a = default_voice_endpoint_presets()
    a.append("mutant")
    assert default_voice_endpoint_presets() == list(DEFAULT_VOICE_ENDPOINT_PRESETS)


def test_appconfig_defaults_to_voice_presets():
    """Зеркало `test_appconfig_defaults_to_presets` (endpoint_presets): AppConfig()
    без config.yml тоже подхватывает дефолты голосовых пресетов, не пустой список."""
    cfg = AppConfig()
    assert [p.name for p in cfg.voice_endpoint_presets] == ["OpenAI", "Groq", "Mistral"]


def test_load_config_flattens_custom_voice_presets_into_config_dict(tmp_path):
    """Зеркало `test_load_config_flattens_custom_presets_into_config_dict`
    (tests/test_endpoint_presets.py): та же регрессия могла повториться на соседнем
    ключе — `flatten_app_config` однажды не пробрасывал `endpoint_presets`, и
    пользовательские пресеты из config.yml до TUI не доезжали, оставался кодовый
    дефолт. У `voice_endpoint_presets` свой ключ схемы — проверяем его отдельно."""
    import config as cfgmod

    conf = tmp_path / "config.yml"
    conf.write_text(
        "voice_endpoint_presets:\n"
        "  - name: My\n    url: http://x/v1/audio/transcriptions\n",
        encoding="utf-8",
    )
    config_dict = cfgmod.load_config(str(conf))

    assert config_dict["voice_endpoint_presets"] == [
        {"name": "My", "url": "http://x/v1/audio/transcriptions"}
    ]
    assert voice_endpoint_presets_from_config(config_dict) == [
        EndpointPreset(name="My", url="http://x/v1/audio/transcriptions")
    ]
