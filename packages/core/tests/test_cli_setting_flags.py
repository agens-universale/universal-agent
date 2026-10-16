"""Общие флаги-настройки gc-acp, gc-chat, gc-a2a: те же уровни, что у gc-cli."""

import argparse

import pytest
from gc_core.config_modules.cli_args import add_setting_flags


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
