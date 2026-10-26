import pytest
from gc_core.config_modules.tui import TuiConfig


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


def test_load_settings_exposes_tui_stream_display(tmp_path):
    """Значение из config.yml доходит до настроек (overlay нейтрализованы)."""
    from gc_core import config as cfgmod

    conf = tmp_path / "config.yml"
    conf.write_text("tui:\n  stream_display: minimal\n", encoding="utf-8")
    tui = cfgmod.load_settings(str(conf)).settings.tui
    assert tui.stream_display == "minimal"


def test_tui_bang_shell_defaults():
    from gc_core.config_modules.tui import BangShellConfig, TuiConfig

    assert TuiConfig().bang_shell == BangShellConfig()
    assert TuiConfig().bang_shell.enabled is True
    assert TuiConfig().bang_shell.timeout_sec is None
    assert TuiConfig().bang_shell.max_output_bytes == 32768
    assert TuiConfig().bang_shell.max_output_lines == 200


def test_load_settings_exposes_tui_bang_shell(tmp_path):
    from gc_core import config as cfgmod

    conf = tmp_path / "config.yml"
    conf.write_text(
        "tui:\n  bang_shell:\n    enabled: false\n    timeout_sec: 30\n",
        encoding="utf-8",
    )
    tui = cfgmod.load_settings(str(conf)).settings.tui
    assert tui.bang_shell.enabled is False
    assert tui.bang_shell.timeout_sec == 30


def test_tui_file_view_defaults():
    from gc_core.config_modules.tui import FileViewConfig, TuiConfig

    assert TuiConfig().file_view == FileViewConfig()
    assert TuiConfig().file_view.enabled is True
    assert TuiConfig().file_view.max_file_bytes == 64 * 1024 * 1024
    assert TuiConfig().file_view.page_lines is None


def test_load_settings_exposes_tui_file_view(tmp_path):
    from gc_core import config as cfgmod

    conf = tmp_path / "config.yml"
    conf.write_text(
        "tui:\n  file_view:\n    enabled: false\n    max_file_bytes: 1024\n    page_lines: 40\n",
        encoding="utf-8",
    )
    tui = cfgmod.load_settings(str(conf)).settings.tui
    assert tui.file_view.enabled is False
    assert tui.file_view.max_file_bytes == 1024
    assert tui.file_view.page_lines == 40


def test_load_settings_file_view_page_lines_null(tmp_path):
    from gc_core import config as cfgmod

    conf = tmp_path / "config.yml"
    conf.write_text(
        "tui:\n  file_view:\n    page_lines: null\n",
        encoding="utf-8",
    )
    tui = cfgmod.load_settings(str(conf)).settings.tui
    assert tui.file_view.page_lines is None
