"""Секция `security.permissions`: схема и шаблоны."""

import pytest
from gc_testkit.settings import make_settings
from pydantic import ValidationError

from gc_core.config_modules.root import AppConfig
from gc_core.security.permission_mode import (
    AVAILABLE_MODES,
    INTERPRETER_COMMANDS,
)
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


def _with_readonly(entries):
    return AppConfig.model_validate(
        {"security": {"permissions": {"readonly_commands": entries}}}
    )


def test_readonly_pair_and_plain_command_are_accepted():
    cfg = _with_readonly(["git log", "tree"])
    assert cfg.security.permissions.readonly_commands == ["git log", "tree"]


def test_single_word_command_with_subcommands_is_rejected():
    """`git` одним словом — это и `git push`: нужна подкоманда."""
    with pytest.raises(ValidationError, match="git log"):
        _with_readonly(["git"])


@pytest.mark.parametrize("name", sorted(INTERPRETER_COMMANDS))
def test_interpreter_cannot_be_a_harmless_command(name):
    with pytest.raises(ValidationError, match="интерпретатор"):
        _with_readonly(["tree", name])


@pytest.mark.parametrize("entry", ["bash -c", "python -m", "env FOO=1"])
def test_interpreter_as_the_first_word_of_a_pair_is_rejected(entry):
    with pytest.raises(ValidationError, match="интерпретатор"):
        _with_readonly([entry])


@pytest.mark.parametrize("entry", ["git -c", "git -C"])
def test_pair_with_a_flag_as_the_second_word_is_rejected(entry):
    """`git -c` — флаг, а не подкоманда: пара совпала бы с любой командой
    git, начинающейся с этого флага (`git -c core.pager=… log`)."""
    with pytest.raises(ValidationError, match="второе слово флаг"):
        _with_readonly([entry])


def test_entry_of_three_or_more_words_is_rejected():
    """Признаются слово и пара «команда подкоманда»; длиннее — не запись."""
    with pytest.raises(ValidationError, match="команда подкоманда"):
        _with_readonly(["git log --oneline"])


def test_interpreter_list_is_the_reference_list():
    assert {"python", "bash", "cmd.exe", "Rscript", "env"} <= (
        INTERPRETER_COMMANDS
    )
    assert len(INTERPRETER_COMMANDS) == 22
