import io
from tui.services.terminal_title import format_terminal_title, set_terminal_title


def test_format_idle():
    assert format_terminal_title() == "✦ gc-ua"
    assert format_terminal_title(None) == "✦ gc-ua"
    assert format_terminal_title("   ") == "✦ gc-ua"


def test_format_with_task():
    assert format_terminal_title("привет мир") == "✦ gc-ua · привет мир"


def test_format_collapses_newlines():
    assert format_terminal_title("строка1\n строка2") == "✦ gc-ua · строка1 строка2"


def test_format_truncates_long_task():
    out = format_terminal_title("a" * 100, max_len=10)
    assert out == "✦ gc-ua · " + "a" * 10 + "…"


def test_set_terminal_title_writes_osc():
    buf = io.StringIO()
    set_terminal_title("✦ gc-ua · x", out=buf)
    assert buf.getvalue() == "\033]0;✦ gc-ua · x\007"


def test_set_terminal_title_uses_utf8_buffer_when_present():
    # Stream with a binary .buffer (like a real console stdout) → emblem written
    # as UTF-8 bytes, bypassing any legacy text encoding (Windows cp1251).
    class _Stream:
        def __init__(self):
            self.buffer = io.BytesIO()
    s = _Stream()
    set_terminal_title("✦ gc-ua · x", out=s)
    assert s.buffer.getvalue() == "\033]0;✦ gc-ua · x\007".encode("utf-8")


def test_set_terminal_title_none_out_is_safe():
    # must not raise when out is None
    set_terminal_title("x", out=None)


def test_base_no_task_no_status():
    assert format_terminal_title() == "✦ gc-ua"


def test_status_without_task():
    assert format_terminal_title(status="[running]") == "✦ gc-ua · [running]"


def test_status_before_prompt():
    out = format_terminal_title("сделай рефактор", status="[running]")
    assert out == "✦ gc-ua · [running] · сделай рефактор"


def test_base_and_status_always_kept_prompt_truncated():
    long = "x" * 200
    out = format_terminal_title(long, status="[running]", max_len=10)
    assert out.startswith("✦ gc-ua · [running] · ")
    assert out.endswith("…")


def test_backward_compatible_no_status():
    assert format_terminal_title("привет") == "✦ gc-ua · привет"
