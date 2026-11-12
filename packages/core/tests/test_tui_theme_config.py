"""tui.theme: тема TUI от терминала (auto) или своя тема GigaCode."""

import pytest
from pydantic import ValidationError

from gc_core.config_modules.tui import TuiConfig


def test_theme_default_is_auto():
    assert TuiConfig().theme == "auto"


def test_theme_gigacode_accepted():
    assert TuiConfig(theme="gigacode").theme == "gigacode"


@pytest.mark.parametrize("value", ["dark", "fixed"])
def test_theme_unknown_value_rejected(value):
    with pytest.raises(ValidationError):
        TuiConfig(theme=value)
