"""Общие флаги-настройки gc-acp, gc-chat, gc-a2a: те же уровни, что у gc-cli."""

import argparse

import pytest
from gc_core.config_modules.cli_args import add_setting_flags
from gc_core.config_modules.model_registry import (
    LEVEL_VALUES,
    validate_level_value,
)


def _parser():
    parser = argparse.ArgumentParser()
    add_setting_flags(parser)
    return parser


@pytest.mark.parametrize("level", ["light", "default", "max", "auto"])
def test_level_flag_accepts_every_level(level):
    assert _parser().parse_args(["--level", level]).level == level


def test_level_flag_rejects_unknown_level():
    with pytest.raises(SystemExit):
        _parser().parse_args(["--level", "turbo"])


def test_level_flag_choices_are_the_single_level_values():
    action = next(
        a for a in _parser()._actions if "--level" in a.option_strings
    )
    assert list(action.choices) == list(LEVEL_VALUES)
    assert [validate_level_value(v) for v in LEVEL_VALUES] == list(
        LEVEL_VALUES
    )
