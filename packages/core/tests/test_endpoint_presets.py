from gc_core.config_modules.endpoint_presets import (
    DEFAULT_ENDPOINT_PRESETS,
    EndpointPreset,
    endpoint_presets_from_config,
)
from gc_core.config_modules.root import AppConfig

_EXPECTED = [
    ("OpenRouter", "https://openrouter.ai/api/v1/chat/completions"),
    ("Cloud.ru", "https://foundation-models.api.cloud.ru/v1/chat/completions"),
    ("RouterAI", "https://routerai.ru/api/v1/chat/completions"),
]


def test_default_presets_names_and_urls():
    """Порядок — часть контракта, а не оформление: поле адреса пусто только на
    первом экране мастера, и там курсор встаёт на первый пункт (field_list
    _pick_index). То есть первый пресет — это предложение по умолчанию тому, кто
    ничего не набрал; дальше идут прочие внешние."""
    assert [(p.name, p.url) for p in DEFAULT_ENDPOINT_PRESETS] == _EXPECTED


def test_appconfig_defaults_to_presets():
    cfg = AppConfig()
    assert [p.name for p in cfg.endpoint_presets] == [n for n, _ in _EXPECTED]


def test_default_presets_are_not_materialized_into_the_template():
    """Шаблоны отдают блок ЗАКОММЕНТИРОВАННЫМ, чтобы каталог провайдеров жил в
    одном месте — в коде — и обновлялся вместе с версией. Развёрнутый блок
    впечатывался бы копией в конфиг каждому при первом запуске, и следующий
    добавленный провайдер до людей уже не доехал бы (живой случай 2026-08-17:
    в песочнице осталось три пресета из четырёх)."""
    from gc_testkit.paths import ASSETS, REPO

    # Шаблон ядра уехал в packages/core/assets/, demo_config.yml остался
    # в корне репозитория — считать оба от __file__ больше нельзя.
    # Правило одно на ВСЕ каталоги пресетов, а не на первый заведённый:
    # голосовой каталог приехал в шаблоны развёрнутым именно потому, что
    # сторож спрашивал про `endpoint_presets`, а `voice_endpoint_presets`
    # под это условие не подпадал.
    for key in ("endpoint_presets", "voice_endpoint_presets"):
        for path in (ASSETS / "config.default.yml", REPO / "demo_config.yml"):
            name = path.name
            text = path.read_text(encoding="utf-8")
            assert f"\n{key}:" not in text, f"{name}: блок {key} развёрнут"
            assert f"# {key}:" in text, (
                f"{name}: нет закомментированного образца {key}"
            )


def test_from_config_missing_key_gives_default():
    assert endpoint_presets_from_config({}) == list(DEFAULT_ENDPOINT_PRESETS)


def test_from_config_reads_user_list():
    cfg = {
        "endpoint_presets": [
            {"name": "My", "url": "http://x/v1/chat/completions"}
        ]
    }
    out = endpoint_presets_from_config(cfg)
    assert out == [
        EndpointPreset(name="My", url="http://x/v1/chat/completions")
    ]


def test_load_config_flattens_custom_presets_into_config_dict(tmp_path):
    """Регрессия (fix Important-1): flatten_app_config не пробрасывал
    endpoint_presets — TUI видел только кодовый дефолт, пользовательские
    пресеты из config.yml были мертвы."""
    from gc_core import config as cfgmod

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
