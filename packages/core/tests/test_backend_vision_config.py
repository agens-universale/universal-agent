"""Флаг `backend.vision` в схеме конфига: общая секция уходит плоским ключом
`backend_vision`, значение модели — в её `backend_overrides` (так их читает
«кто смотрит», `gc_core.vision.audience`)."""

from gc_core.config import flatten_app_config
from gc_core.config_modules.root import AppConfig


def test_the_general_flag_is_flattened():
    assert (
        flatten_app_config(AppConfig(backend={"vision": False}))[
            "backend_vision"
        ]
        is False
    )


def test_the_flag_is_unset_by_default():
    assert flatten_app_config(AppConfig())["backend_vision"] is None


def test_a_model_flag_lands_in_its_backend_overrides():
    app_cfg = AppConfig(
        models={
            "gc": {
                "model": "gc_v1.6",
                "url": "http://proxy/v1/chat/completions",
                "token": "",
                "backend": {"vision": False},
            },
        },
        slots={"default": "gc"},
    )

    assert flatten_app_config(app_cfg)["models"]["gc"][
        "backend_overrides"
    ] == {"vision": False}
