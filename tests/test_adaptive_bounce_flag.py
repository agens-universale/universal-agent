"""Режим tui.text_answer_mode: схема (дефолт judge) + flatten в config_dict."""
import pytest
from config_modules.tui import TuiConfig


def test_tui_config_text_answer_mode_default_judge():
    assert TuiConfig().text_answer_mode == "judge"


def test_tui_config_text_answer_mode_accepts_strict_and_always():
    assert TuiConfig(text_answer_mode="strict").text_answer_mode == "strict"
    assert TuiConfig(text_answer_mode="always").text_answer_mode == "always"


def test_tui_config_text_answer_mode_rejects_unknown():
    with pytest.raises(Exception):
        TuiConfig(text_answer_mode="bogus")


def test_flatten_exposes_tui_text_answer_mode(tmp_path, monkeypatch):
    """Значение из config.yml доходит до плоского config_dict (overlay-слои нейтрализованы)."""
    import config as cfgmod
    import mcp_runtime.paths as mpaths

    monkeypatch.setattr(cfgmod, "_project_local_config_path", lambda: None)
    monkeypatch.setattr(mpaths, "managed_config_path", lambda: tmp_path / "none.yml")

    conf = tmp_path / "config.yml"
    conf.write_text("tui:\n  text_answer_mode: always\n", encoding="utf-8")  # не дефолт → доказывает flatten
    cfg = cfgmod.load_config(str(conf))
    assert cfg["tui_text_answer_mode"] == "always"
