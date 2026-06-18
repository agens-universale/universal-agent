from config_modules.tui import TuiConfig


def test_tui_defaults():
    c = TuiConfig()
    assert c.show_reasoning_line is True
    assert c.show_stream_answer is False


def test_tui_overrides():
    c = TuiConfig(show_reasoning_line=False, show_stream_answer=True)
    assert c.show_reasoning_line is False
    assert c.show_stream_answer is True
