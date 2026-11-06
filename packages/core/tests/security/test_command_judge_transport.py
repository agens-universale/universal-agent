"""Судья модерации команд доступен, если у ``command_judge`` есть адрес
модели — легаси ``backend.completions_url`` или запись реестра ``models``.

Конфиг только с реестром оставляет ``backend.completions_url`` пустым. Судья
считался по нему одному «не настроенным», и fail-closed политика блокировала
модерируемые команды (под Temporal — ``exec_python`` субагента).
"""

from types import SimpleNamespace

import pytest
from gc_core.model_router import ModelRouter, consumer_has_transport
from gc_core.security import command_moderation
from gc_testkit.settings import fake_ctx, make_settings

_REGISTRY = {
    "models": {
        "cheap": {
            "model": "vendor/cheap",
            "url": "http://models.local/v1/chat/completions",
            "token": "",
        }
    },
    "slots": {"light": "cheap", "default": "cheap", "max": "cheap"},
}


def _ctx(config, *, router=True):
    loaded = make_settings(config)
    attrs = {}
    if router:
        attrs["model_router"] = ModelRouter(
            loaded, choice_provider=lambda: "default"
        )
    return fake_ctx(loaded=loaded, **attrs)


def _judge(ctx):
    return command_moderation._default_judge_client(
        (), {"ctx": ctx, "state": SimpleNamespace()}
    )


@pytest.mark.parametrize("router", [True, False])
def test_registry_only_config_has_a_judge(router):
    ctx = _ctx(_REGISTRY, router=router)
    assert ctx.settings.backend.completions_url == ""
    assert consumer_has_transport(ctx.settings, "command_judge")
    assert callable(_judge(ctx))


def test_no_address_anywhere_keeps_the_judge_unavailable():
    ctx = _ctx({})
    assert not consumer_has_transport(ctx.settings, "command_judge")
    with pytest.raises(command_moderation.JudgeUnavailable):
        _judge(ctx)


def test_legacy_backend_url_is_enough_without_asking_the_router():
    ctx = _ctx({"backend.completions_url": "http://legacy.local/v1"})
    ctx.model_router = SimpleNamespace(resolve=None)  # не зовётся
    assert callable(_judge(ctx))


def test_explicit_flag_of_the_context_wins():
    """Под Temporal флаг ставит воркер (снимок настроек) — он и решает."""
    ctx = _ctx(_REGISTRY)
    ctx.model_transport_configured = False
    with pytest.raises(command_moderation.JudgeUnavailable):
        _judge(ctx)
