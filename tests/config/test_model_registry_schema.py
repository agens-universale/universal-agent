import pytest
from pydantic import ValidationError

from config_modules.model_registry import ModelEntry, AssignConfig
from config_modules.model_sections_validation import validate_model_sections
from config_modules.root import AppConfig


def test_models_slots_assign_parse_minimal():
    cfg = AppConfig.model_validate({
        "models": {"glm": {"url": "http://x/v1/chat/completions"}},
        "slots": {"default": "glm"},
        "assign": {"finish_gate": "light"},
    })
    assert cfg.models["glm"].model is None          # id = алиас (сахар)
    assert cfg.slots.default == "glm"


def test_reserved_alias_rejected():
    with pytest.raises(ValidationError, match="зарезервировано"):
        AppConfig.model_validate({"models": {"auto": {"url": "http://x"}}})


def test_token_authorization_conflict():
    with pytest.raises(ValidationError, match="одно"):
        ModelEntry.model_validate({
            "token": "sk-1",
            "backend": {"chat_completion_headers": {"Authorization": "Bearer y"}},
        })


def test_url_completions_url_conflict():
    with pytest.raises(ValidationError):
        ModelEntry.model_validate({"url": "http://a", "backend": {"completions_url": "http://b"}})


def test_level_auto_friendly_error():
    with pytest.raises(ValidationError, match="авто-выбора"):
        AppConfig.model_validate({"level": "auto"})


def test_level_rejects_any_value_outside_the_three_levels():
    """`level` принимает ровно light|default|max в волнах A–B. У `auto` свой
    дружелюбный текст (тест выше); здесь — общая ветка: опечатка в ключе не
    должна протекать до Router'а, где «нет такого слота» превратилось бы в
    фоллбэк на default и молча работало бы не тем уровнем."""
    with pytest.raises(ValidationError, match="light"):
        AppConfig.model_validate({"level": "fast"})


def test_main_assign_forbidden():
    with pytest.raises(ValidationError):
        AssignConfig.model_validate({"main": "light"})


def test_unknown_assign_key_forbidden():
    """§14: «неизвестный ключ assign — строгая ошибка». Имя субагента стережёт
    фаза 2, а опечатку в имени ПОТРЕБИТЕЛЯ (`finish_gates`) ловить некому, кроме
    `extra="forbid"` схемы: без него строка тихо не действовала бы, а
    пользователь считал бы судью привязанным."""
    with pytest.raises(ValidationError):
        AssignConfig.model_validate({"finish_gates": "light"})


def test_one_model_may_stand_in_two_slots():
    """§14: «одна модель в двух слотах». Слоты — УКАЗАТЕЛИ, а не владение (§4),
    поэтому дублирующая ссылка легальна: типовой сетап «есть только быстрая и
    умная» ставит одну модель и на default, и на max."""
    cfg = AppConfig.model_validate({
        "models": {"fast": {"url": "http://fast/v1"}, "smart": {"url": "http://smart/v1"}},
        "slots": {"light": "fast", "default": "smart", "max": "smart"},
    })
    assert cfg.slots.default == "smart"
    assert cfg.slots.max == "smart"
    assert validate_model_sections(cfg) == []


def test_token_bearer_normalization():
    assert ModelEntry.model_validate({"token": "sk-1"}).authorization_header == "Bearer sk-1"
    assert ModelEntry.model_validate({"token": "Bearer sk-1"}).authorization_header == "Bearer sk-1"
    assert ModelEntry.model_validate({"token": ""}).authorization_header == ""
