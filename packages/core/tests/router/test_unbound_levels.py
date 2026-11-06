"""Уровень служебных потребителей без привязки (спека PR140 Р1): судьи и
суммаризация не опускаются и не дорожают вместе с исполнителем; явная привязка
сильнее; судей привязанного ребёнка определяет кодовый уровень, а не его пин.
Кодовый уровень действует, только когда его слот задан: иначе потребитель идёт
как вне таблицы (пин ребёнка, затем активный уровень), а не фоллбэком уровня."""

import pytest

from gc_core.config_modules.root import AppConfig
from gc_core.model_router import (
    CONSUMERS,
    ModelRouter,
    ResolvedModel,
    fixed_unbound_level,
    unbound_level,
)

REGISTRY = {
    "models": {"fast": {}, "mid": {}, "big": {}},
    "slots": {"light": "fast", "default": "mid", "max": "big"},
}
# Слоты light и max без default: фоллбэк уровня default по матрице — max.
NO_DEFAULT = {
    "models": {"fast": {}, "big": {}},
    "slots": {"light": "fast", "max": "big"},
}
FIXED = (
    "finish_gate",
    "text_answer_judge",
    "command_judge",
    "summary",
    "routing_judge",
)
FOLLOWING = ("intent_scope", "memory_compaction", "vision")


def _router(level, assign=None, unbound_override=None, registry=REGISTRY):
    raw = dict(registry)
    if assign is not None:
        raw["assign"] = assign
    return ModelRouter(
        AppConfig.model_validate(raw),
        choice_provider=lambda: level,
        unbound_override=unbound_override,
    )


def _pin(alias="pinned"):
    return ResolvedModel(
        alias=alias,
        model=f"{alias}-model",
        url=f"http://{alias}.local/v1/chat/completions",
        authorization=None,
        backend_overrides={},
        tariff=None,
        source="test-pin",
    )


def test_unbound_level_names_exactly_the_fixed_consumers():
    assert [c for c in CONSUMERS if unbound_level(c)] == list(FIXED)
    assert {unbound_level(c) for c in FIXED} == {"default"}
    assert unbound_level("main") is None
    assert unbound_level("subagent:subagent_worker") is None


@pytest.mark.parametrize("consumer", FIXED)
def test_fixed_unbound_level_requires_the_level_slot(consumer):
    assert fixed_unbound_level(consumer, {"default": "mid"}) == "default"
    no_default = {"light": "fast", "max": "big"}
    assert fixed_unbound_level(consumer, no_default) is None
    assert fixed_unbound_level(consumer, {"default": None}) is None
    assert fixed_unbound_level(consumer, {}) is None


def test_fixed_unbound_level_is_none_outside_the_table():
    assert fixed_unbound_level("vision", {"default": "mid"}) is None
    assert fixed_unbound_level("main", {"default": "mid"}) is None


@pytest.mark.parametrize("level", ["light", "max"])
@pytest.mark.parametrize("consumer", FIXED)
def test_without_default_slot_fixed_consumer_follows_active_level(
    consumer, level
):
    """Ручной light без слота default: судьи на light, а не на max фоллбэком."""
    expected = {"light": "fast", "max": "big"}[level]
    resolved = _router(level, registry=NO_DEFAULT).resolve(consumer)
    assert resolved.alias == expected
    assert resolved.source.startswith("активный ")


@pytest.mark.parametrize("consumer", FIXED)
def test_without_default_slot_an_explicit_binding_still_wins(consumer):
    router = _router("light", {consumer: "max"}, registry=NO_DEFAULT)
    assert router.resolve(consumer).alias == "big"
    # Явная привязка к уровню default резолвится матрицей, как любая привязка.
    router = _router("light", {consumer: "default"}, registry=NO_DEFAULT)
    assert router.resolve(consumer).alias == "big"


@pytest.mark.parametrize("consumer", FIXED)
def test_without_default_slot_a_pinned_child_judges_go_to_the_pin(consumer):
    router = _router("light", unbound_override=_pin(), registry=NO_DEFAULT)
    assert router.resolve(consumer).alias == "pinned"


@pytest.mark.parametrize("level", ["light", "max"])
@pytest.mark.parametrize("consumer", FIXED)
def test_fixed_consumer_without_binding_goes_default(consumer, level):
    resolved = _router(level).resolve(consumer)
    assert resolved.alias == "mid"
    assert consumer in resolved.source and "default" in resolved.source


@pytest.mark.parametrize("consumer", FOLLOWING)
def test_other_consumers_still_follow_the_active_level(consumer):
    assert _router("max").resolve(consumer).alias == "big"
    assert _router("light").resolve(consumer).alias == "fast"


@pytest.mark.parametrize("consumer", FIXED)
def test_an_explicit_binding_beats_the_fixed_level(consumer):
    assert _router("light", {consumer: "max"}).resolve(consumer).alias == "big"
    assert (
        _router("light", {consumer: "fast"}).resolve(consumer).alias == "fast"
    )


@pytest.mark.parametrize("assign", [{}, {"finish_gate": None}])
def test_explicit_null_binding_is_the_same_as_no_binding(assign):
    assert _router("max", assign).resolve("finish_gate").alias == "mid"


@pytest.mark.parametrize("consumer", FIXED)
def test_a_pinned_child_judges_go_default_not_to_the_pin(consumer):
    assert (
        _router("default", unbound_override=_pin()).resolve(consumer).alias
        == "mid"
    )


def test_a_pinned_child_still_runs_main_and_other_consumers_on_the_pin():
    router = _router("default", unbound_override=_pin())
    assert router.resolve("main").alias == "pinned"
    for consumer in FOLLOWING:
        assert router.resolve(consumer).alias == "pinned"


def test_a_pinned_child_with_its_own_judge_binding_uses_the_binding():
    router = _router(
        "default", {"finish_gate": "max"}, unbound_override=_pin()
    )
    assert router.resolve("finish_gate").alias == "big"


def test_auto_judge_is_not_a_consumer():
    with pytest.raises(ValueError, match="неизвестный consumer"):
        _router("default").resolve("auto_judge")
