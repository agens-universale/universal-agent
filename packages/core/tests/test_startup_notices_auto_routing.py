"""Уведомления о недоступном автовыборе: typed settings и CLI provenance."""

import pytest

from gc_core.startup_notices import (
    auto_routing_unavailable_notice,
    auto_routing_unavailable_reason_text,
    collect_startup_notices,
    unknown_model_choice_notice,
)
from gc_testkit.settings import fake_ctx, make_settings


def _cfg(**overrides):
    return make_settings(
        {
            "model": "auto",
            "agent.auto_routing": True,
            "backend.api_mode": "chat_completions",
            "models": {"fast": {}, "mid": {}, "big": {}},
            "slots": {"light": "fast", "default": "mid", "max": "big"},
            **overrides,
        }
    )


def test_no_notice_when_auto_available_or_not_selected():
    assert auto_routing_unavailable_notice(_cfg()) is None
    assert (
        auto_routing_unavailable_notice(
            _cfg(
                **{
                    "model": "max",
                    "agent.auto_routing": False,
                }
            )
        )
        is None
    )


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"agent.auto_routing": False}, "выключен настройкой auto_routing"),
        ({"backend.api_mode": "responses"}, "режим API responses"),
        (
            {"models": {"only": {}}, "slots": {"default": "only"}},
            "во всех уровнях одна модель",
        ),
        ({"models": {}, "slots": {}}, "во всех уровнях одна модель"),
    ],
)
def test_notice_names_unavailability_and_the_effective_fallback(
    overrides, reason
):
    notice = auto_routing_unavailable_notice(_cfg(**overrides))
    assert notice.level == "info"
    assert notice.code == "auto_routing_unavailable"
    assert notice.message == (
        f"автовыбор моделей недоступен: {reason} — работает модель слота default"
    )


@pytest.mark.parametrize("source_kind", ["loaded", "app_config", "context"])
def test_notice_accepts_typed_config_sources(source_kind):
    loaded = _cfg(**{"agent.auto_routing": False})
    source = {
        "loaded": loaded,
        "app_config": loaded.settings,
        "context": fake_ctx(loaded=loaded),
    }[source_kind]
    notice = auto_routing_unavailable_notice(source)
    assert notice.code == "auto_routing_unavailable"
    assert "выключен настройкой auto_routing" in notice.message


def test_cli_model_override_provenance_disables_levels():
    loaded = _cfg().with_overrides(
        {"agent.model": "forced-model"}, layer="cli"
    )
    notice = auto_routing_unavailable_notice(loaded)
    assert notice.message == (
        "автовыбор моделей недоступен: уровни отключены флагом --model "
        "— работает модель из --model"
    )


def test_model_from_base_config_does_not_force_legacy():
    loaded = _cfg(**{"agent.model": "legacy-fallback"})
    assert loaded.provenance.source("agent.model").layer == "base"
    assert auto_routing_unavailable_notice(loaded) is None


def test_notice_names_an_alternative_when_explicit_pair_is_one_model():
    loaded = _cfg(
        **{
            "agent.auto_routing_safe_level": "default",
            "models": {"mid": {}, "big": {}},
            "slots": {"default": "mid", "max": "big"},
        }
    )
    notice = auto_routing_unavailable_notice(loaded)
    assert notice.message == (
        "автовыбор моделей недоступен: в паре light+default одна модель; "
        "другая пара — auto_routing_safe_level: max — работает модель слота default"
    )


def test_auto_pair_uses_two_available_models_without_a_notice():
    loaded = _cfg(
        **{
            "models": {"mid": {}, "big": {}},
            "slots": {"default": "mid", "max": "big"},
        }
    )
    assert loaded.settings.agent.auto_routing_safe_level == "auto"
    assert auto_routing_unavailable_notice(loaded) is None


def test_reason_phrase_is_the_notice_text_without_the_level_tail():
    assert auto_routing_unavailable_reason_text(
        "выключен настройкой auto_routing"
    ) == ("автовыбор моделей недоступен: выключен настройкой auto_routing")


@pytest.mark.parametrize("force_legacy", [False, True])
def test_collect_includes_one_routing_notice_and_preserves_cli_provenance(
    force_legacy,
):
    loaded = _cfg(**{"agent.auto_routing": False})
    reason = "выключен настройкой auto_routing"
    tail = "модель слота default"
    if force_legacy:
        loaded = loaded.with_overrides(
            {"agent.model": "forced-model"}, layer="cli"
        )
        reason = "уровни отключены флагом --model"
        tail = "модель из --model"
    notices = collect_startup_notices(loaded, set(), None, is_tui=False)
    routing = [
        notice
        for notice in notices
        if notice.code == "auto_routing_unavailable"
    ]
    assert len(routing) == 1
    assert routing[0].message == (
        f"автовыбор моделей недоступен: {reason} — работает {tail}"
    )


def test_unknown_model_choice_notice_names_both_models():
    cfg = _cfg(model="ghost")
    notice = unknown_model_choice_notice(cfg)
    assert notice is not None
    assert notice.level == "info"
    assert notice.message == (
        "модель ghost не найдена в реестре — работаю моделью mid"
    )


@pytest.mark.parametrize("choice", ["auto", "default", "light", "big"])
def test_no_notice_for_known_choice(choice):
    assert unknown_model_choice_notice(_cfg(model=choice)) is None


def test_no_unknown_model_notice_without_registry():
    cfg = make_settings({"model": "ghost", "agent.model": "legacy"})
    assert unknown_model_choice_notice(cfg) is None


def test_no_unknown_model_notice_under_legacy_force():
    loaded = _cfg(model="ghost").with_overrides(
        {"agent.model": "raw-id"}, layer="cli"
    )
    assert unknown_model_choice_notice(loaded) is None
