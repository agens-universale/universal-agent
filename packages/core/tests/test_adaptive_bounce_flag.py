"""Режим tui.text_answer_mode: схема (дефолт judge) + загрузка из config.yml."""

import pytest
from gc_core.config_modules.tui import TuiConfig


def test_tui_config_text_answer_mode_default_judge():
    assert TuiConfig().text_answer_mode == "judge"


def test_tui_config_text_answer_mode_accepts_deny_and_allow():
    assert TuiConfig(text_answer_mode="deny").text_answer_mode == "deny"
    assert TuiConfig(text_answer_mode="allow").text_answer_mode == "allow"


def test_tui_config_text_answer_mode_rejects_unknown():
    with pytest.raises(Exception):
        TuiConfig(text_answer_mode="bogus")


def test_load_settings_exposes_tui_text_answer_mode(tmp_path):
    """Значение из config.yml доходит до настроек (overlay-слои нейтрализованы)."""
    from gc_core import config as cfgmod

    conf = tmp_path / "config.yml"
    conf.write_text(
        "tui:\n  text_answer_mode: allow\n", encoding="utf-8"
    )  # не дефолт → доказывает, что значение доехало
    loaded = cfgmod.load_settings(str(conf))
    assert loaded.settings.tui.text_answer_mode == "allow"
