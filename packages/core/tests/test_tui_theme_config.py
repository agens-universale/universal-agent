"""tui.theme: тема TUI от терминала (auto) или фиксированная (fixed)."""

import pytest
from pydantic import ValidationError

from gc_core.config_modules.tui import TuiConfig


def test_theme_default_is_auto():
    assert TuiConfig().theme == "auto"


def test_theme_fixed_accepted():
    assert TuiConfig(theme="fixed").theme == "fixed"


def test_theme_unknown_value_rejected():
    with pytest.raises(ValidationError):
        TuiConfig(theme="dark")
