"""Секция `security.permissions`: схема и шаблоны."""

import pytest
from gc_testkit.settings import make_settings
from pydantic import ValidationError

from gc_core.config_modules.root import AppConfig
from gc_core.security.permission_mode import AVAILABLE_MODES
from gc_core.settings import is_explicit


def test_permissions_defaults():
    permissions = AppConfig().security.permissions
    assert permissions.default_mode == "edit"
    assert permissions.readonly_commands == []


def test_permissions_from_config():
    cfg = AppConfig.model_validate(
        {
            "security": {
                "permissions": {
                    "default_mode": "manual",
                    "readonly_commands": ["rg"],
                }
            }
        }
    )
    assert cfg.security.permissions.default_mode == "manual"
    assert cfg.security.permissions.readonly_commands == ["rg"]


def test_mode_value_is_normalised():
    cfg = AppConfig.model_validate(
        {"security": {"permissions": {"default_mode": " Manual "}}}
    )
    assert cfg.security.permissions.default_mode == "manual"


@pytest.mark.parametrize("value", ["турбо", "accept_edits", "bypass", ""])
def test_unknown_mode_rejected(value):
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"security": {"permissions": {"default_mode": value}}}
        )


def test_every_available_mode_accepted_by_schema():
    for mode in AVAILABLE_MODES:
        cfg = AppConfig.model_validate(
            {"security": {"permissions": {"default_mode": mode.value}}}
        )
        assert cfg.security.permissions.default_mode == mode.value
    assert {m.value for m in AVAILABLE_MODES} == {
        "edit",
        "manual",
        "plan",
        "auto",
    }


def test_schema_gate_follows_available_modes(monkeypatch):
    """Валидатор отсекает режим, которого нет в сборке: сужаем кортеж и
    проверяем сам гейт — тест переживёт появление новых режимов."""
    import gc_core.security.permission_mode as pm

    monkeypatch.setattr(pm, "AVAILABLE_MODES", (pm.Mode.EDIT,))
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"security": {"permissions": {"default_mode": "manual"}}}
        )


def test_default_mode_is_not_explicit_until_the_user_sets_it():
    assert (
        is_explicit(AppConfig(), "security.permissions.default_mode") is False
    )
    loaded = make_settings({"security.permissions.default_mode": "manual"})
    assert (
        is_explicit(loaded.settings, "security.permissions.default_mode")
        is True
    )
