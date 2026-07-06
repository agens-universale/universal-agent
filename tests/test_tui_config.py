import pytest
from config_modules.tui import TuiConfig


def test_tui_default_stream_display_minimal():
    assert TuiConfig().stream_display == "minimal"


def test_tui_stream_display_accepts_three_values():
    for v in ("minimal", "answer", "answer_reasoning"):
        assert TuiConfig(stream_display=v).stream_display == v


def test_tui_stream_display_rejects_unknown():
    with pytest.raises(Exception):
        TuiConfig(stream_display="loud")


def test_tui_rejects_removed_flags():
    with pytest.raises(Exception):
        TuiConfig(show_stream_answer=True)
    with pytest.raises(Exception):
        TuiConfig(show_reasoning_line=False)


def test_flatten_exposes_tui_stream_display(tmp_path, monkeypatch):
    """Значение из config.yml доходит до плоского config_dict (overlay нейтрализованы)."""
    import config as cfgmod
    import mcp_runtime.paths as mpaths

    monkeypatch.setattr(cfgmod, "_project_local_config_path", lambda: None)
    monkeypatch.setattr(mpaths, "managed_config_path", lambda: tmp_path / "none.yml")

    conf = tmp_path / "config.yml"
    conf.write_text("tui:\n  stream_display: minimal\n", encoding="utf-8")
    cfg = cfgmod.load_config(str(conf))
    assert cfg["tui_stream_display"] == "minimal"
