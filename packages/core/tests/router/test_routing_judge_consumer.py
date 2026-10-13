"""Потребитель `routing_judge`: привязка, фиксированный уровень без привязки, учёт."""
import pytest

from gc_core.config_modules.root import AppConfig
from gc_core.model_router import UNBOUND_CONSUMER_LEVEL, ModelRouter
from gc_core.usage.account import CATEGORY_ROUTING_JUDGE, category_for_request_label

REGISTRY = {
    "models": {"fast": {}, "mid": {}, "big": {}},
    "slots": {"light": "fast", "default": "mid", "max": "big"},
}


def _router(level, assign=None):
    snapshot = dict(REGISTRY)
    if assign is not None:
        snapshot["assign"] = assign
    return ModelRouter(AppConfig.model_validate(snapshot), level_provider=lambda: level)


def test_an_unbound_judge_goes_default_not_the_tick_level():
    """Арбитр не следует за тем, кого судит: на такте max он всё равно default."""
    assert UNBOUND_CONSUMER_LEVEL == {"routing_judge": "default"}
    assert _router("max").resolve("routing_judge").alias == "mid"
    assert _router("light").resolve("routing_judge").alias == "mid"
    assert _router("max").resolve("main").alias == "big"


def test_a_binding_still_wins_over_the_fixed_level():
    assert _router("light", {"routing_judge": "max"}).resolve("routing_judge").alias == "big"
    assert _router("light", {"routing_judge": "fast"}).resolve("routing_judge").alias == "fast"


def test_the_binding_is_a_scalar_field_of_the_schema():
    cfg = AppConfig.model_validate(
        {**REGISTRY, "assign": {"routing_judge": "default"}})
    assert cfg.assign.routing_judge == "default"
    assert AppConfig().assign.routing_judge is None


def test_auto_is_still_forbidden_on_the_right_hand_side():
    """Запрет `auto` справа живёт не в pydantic-схеме (`AssignConfig` проверяет
    только отсутствие `main`), а в `validate_model_sections`. Этот же тест
    доказывает, что имя попало в `_ASSIGN_SCALAR_FIELDS`: без записи в кортеже
    цикл валидатора до поля не дойдёт и отказа не будет."""
    from gc_core.config_modules.model_sections_validation import validate_model_sections
    cfg = AppConfig.model_validate({**REGISTRY, "assign": {"routing_judge": "auto"}})
    with pytest.raises(ValueError) as exc:
        validate_model_sections(cfg)
    assert "assign.routing_judge" in str(exc.value) and "auto" in str(exc.value)


def test_a_normal_binding_passes_the_section_validator():
    """Обратная сторона: живое значение отказа не вызывает (валидатор возвращает
    только предупреждения)."""
    from gc_core.config_modules.model_sections_validation import validate_model_sections
    cfg = AppConfig.model_validate({**REGISTRY, "assign": {"routing_judge": "default"}})
    assert validate_model_sections(cfg) == []


def test_usage_of_the_judge_has_its_own_category():
    assert CATEGORY_ROUTING_JUDGE == "routing_judge"
    assert category_for_request_label("routing_judge") == CATEGORY_ROUTING_JUDGE
    assert category_for_request_label("intent_guard_finish_gate") == "finish_gate"


def test_the_consumer_has_a_russian_label_for_the_degradation_notice():
    from gc_core.utils.llm_chat_client import CONSUMER_RU_LABEL
    assert CONSUMER_RU_LABEL["routing_judge"] == "судья автовыбора"
