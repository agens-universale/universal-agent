"""Режим самопроверки ``finish_gate_mode``: ``off`` · ``smart`` · ``always``.

Прежнее имя третьего значения ``on`` принимается как ``always`` — старые
конфиги и сохранённые настройки сессии грузятся без правки."""

import pytest
from gc_core.config_modules.acp import AcpConfig
from gc_core.config_modules.tui import TuiConfig
from pydantic import ValidationError


@pytest.mark.parametrize("section", [TuiConfig, AcpConfig])
@pytest.mark.parametrize("value", ["off", "smart", "always"])
def test_gate_values(section, value):
    assert section(finish_gate_mode=value).finish_gate_mode == value


@pytest.mark.parametrize("section", [TuiConfig, AcpConfig])
def test_legacy_on_is_always(section):
    assert section(finish_gate_mode="on").finish_gate_mode == "always"


@pytest.mark.parametrize("section", [TuiConfig, AcpConfig])
def test_unknown_gate_value_rejected(section):
    with pytest.raises(ValidationError):
        section(finish_gate_mode="strict")
