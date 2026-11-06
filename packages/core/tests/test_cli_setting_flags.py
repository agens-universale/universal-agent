"""Флаг --model — единственный выбор модели у gc-acp, gc-chat, gc-a2a."""

import argparse

import pytest
from gc_core.config import cli_forces_legacy_model
from gc_core.config_modules.cli_args import (
    add_setting_flags,
    apply_entry_overrides,
    setting_flag_overrides,
)
from gc_testkit.settings import make_settings

_REGISTRY = {
    "models": {"flash": {"model": "m"}, "pro": {"model": "p"}},
    "slots": {"default": "flash"},
}


def _parser():
    parser = argparse.ArgumentParser()
    add_setting_flags(parser)
    return parser


def _applied(value, **base):
    args = _parser().parse_args(["--model", value])
    return apply_entry_overrides(
        make_settings(base or _REGISTRY), setting_flag_overrides(args)
    )


def test_model_flag_auto_is_a_choice():
    loaded = _applied("auto")
    assert loaded.settings.model == "auto"
    assert not cli_forces_legacy_model(loaded)


def test_model_flag_alias_is_a_choice():
    loaded = _applied("pro")
    assert loaded.settings.model == "pro"
    assert not cli_forces_legacy_model(loaded)


def test_model_flag_level_word_is_a_choice():
    loaded = _applied("max")
    assert loaded.settings.model == "max"
    assert not cli_forces_legacy_model(loaded)


def test_model_flag_raw_id_forces_legacy():
    loaded = _applied("raw-model-id")
    assert loaded.settings.agent.model == "raw-model-id"
    assert loaded.settings.model != "raw-model-id"
    assert cli_forces_legacy_model(loaded)


def test_model_flag_with_empty_registry_is_plain_legacy():
    loaded = _applied("max", **{"agent.model": "x"})
    assert loaded.settings.agent.model == "max"
    assert not cli_forces_legacy_model(loaded)


def test_level_flag_errors_with_model_hint(capsys):
    with pytest.raises(SystemExit) as exc:
        _parser().parse_args(["--level", "max"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "флаг --level убран — используйте --model" in err


def test_level_flag_is_hidden_from_help():
    assert "--level" not in _parser().format_help()
