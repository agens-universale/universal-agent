import pytest
from pydantic import ValidationError

from config_modules.model_registry import ModelEntry, AssignConfig
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


def test_main_assign_forbidden():
    with pytest.raises(ValidationError):
        AssignConfig.model_validate({"main": "light"})


def test_token_bearer_normalization():
    assert ModelEntry.model_validate({"token": "sk-1"}).authorization_header == "Bearer sk-1"
    assert ModelEntry.model_validate({"token": "Bearer sk-1"}).authorization_header == "Bearer sk-1"
    assert ModelEntry.model_validate({"token": ""}).authorization_header == ""
