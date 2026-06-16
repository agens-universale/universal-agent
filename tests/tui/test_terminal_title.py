import io
from tui.services.terminal_title import format_terminal_title, set_terminal_title


def test_format_idle():
    assert format_terminal_title() == "✦ GigaCode"
    assert format_terminal_title(None) == "✦ GigaCode"
    assert format_terminal_title("   ") == "✦ GigaCode"


def test_format_with_task():
    assert format_terminal_title("привет мир") == "✦ GigaCode · привет мир"


def test_format_collapses_newlines():
    assert format_terminal_title("строка1\n строка2") == "✦ GigaCode · строка1 строка2"


def test_format_truncates_long_task():
    out = format_terminal_title("a" * 100, max_len=10)
    assert out == "✦ GigaCode · " + "a" * 10 + "…"


def test_set_terminal_title_writes_osc():
    buf = io.StringIO()
    set_terminal_title("✦ GigaCode · x", out=buf)
    assert buf.getvalue() == "\033]0;✦ GigaCode · x\007"


def test_set_terminal_title_none_out_is_safe():
    # must not raise when out is None
    set_terminal_title("x", out=None)
