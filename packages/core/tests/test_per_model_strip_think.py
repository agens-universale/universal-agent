"""strip_think на алиасе: отсутствие ключа — глобальный флаг хода."""

from gc_core.config import flatten_app_config
from gc_core.config_modules.root import AppConfig
from gc_core.config_modules.strip_tags_projection import (
    persist_reasoning_content_for_model,
)
from gc_core.model_router import ModelRouter, ResolvedModel


def _cfg(raw: dict) -> dict:
    return flatten_app_config(AppConfig.model_validate(raw))


def test_alias_strip_think_overrides_the_global_flag():
    cfg = _cfg(
        {
            "strip_tags_projection": {"strip_think": True},
            "models": {
                "keep": {
                    "url": "http://keep/v1/chat/completions",
                    "token": "",
                    "strip_think": False,
                },
                "drop": {
                    "url": "http://drop/v1/chat/completions",
                    "token": "",
                    "strip_think": True,
                },
                "inherit": {
                    "url": "http://inherit/v1/chat/completions",
                    "token": "",
                },
            },
            "slots": {
                "light": "keep",
                "default": "drop",
                "max": "inherit",
            },
        }
    )
    router = ModelRouter(cfg, level_provider=lambda: "light")
    keep = router.resolve("main")
    assert keep.strip_think is False
    assert persist_reasoning_content_for_model(keep, cfg) is True

    router = ModelRouter(cfg, level_provider=lambda: "default")
    drop = router.resolve("main")
    assert drop.strip_think is True
    assert persist_reasoning_content_for_model(drop, cfg) is False

    router = ModelRouter(cfg, level_provider=lambda: "max")
    inherit = router.resolve("main")
    assert inherit.strip_think is None
    assert persist_reasoning_content_for_model(inherit, cfg) is False
    assert "strip_think" not in cfg["models"]["inherit"]


def test_missing_alias_follows_global_false():
    cfg = {"strip_tags_projection": {"strip_think": False}}
    legacy = ResolvedModel(
        alias=None,
        model="m",
        url="http://x",
        authorization=None,
        backend_overrides={},
        tariff=None,
        source="legacy",
    )
    assert persist_reasoning_content_for_model(legacy, cfg) is True
    assert persist_reasoning_content_for_model(None, cfg) is True
