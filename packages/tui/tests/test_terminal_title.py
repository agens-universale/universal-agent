import io

from gc_tui.services.terminal_title import (
    format_terminal_title,
    set_terminal_title,
)


def test_format_idle():
    assert format_terminal_title() == "✦ GigaCode"
    assert format_terminal_title(None) == "✦ GigaCode"
    assert format_terminal_title("   ") == "✦ GigaCode"


def test_format_with_task_no_status():
    # Статуса нет → глифа нет, разделитель ·.
    assert format_terminal_title("привет мир") == "✦ GigaCode · привет мир"


def test_format_collapses_newlines():
    assert (
        format_terminal_title("строка1\n строка2")
        == "✦ GigaCode · строка1 строка2"
    )


def test_format_truncates_long_task():
    out = format_terminal_title("a" * 100, max_len=10)
    assert out == "✦ GigaCode · " + "a" * 10 + "…"


def test_status_glyph_is_separator():
    # Глиф статуса сам разделяет имя и промпт — без ·.
    assert (
        format_terminal_title("сделай рефактор", "running")
        == "✦ GigaCode ▸ сделай рефактор"
    )
    assert (
        format_terminal_title("готово", "completed") == "✦ GigaCode ✓ готово"
    )
    assert format_terminal_title("стоп", "stopped") == "✦ GigaCode ■ стоп"
    assert format_terminal_title("упало", "failed") == "✦ GigaCode ✗ упало"
    assert format_terminal_title("ждём", "asking") == "✦ GigaCode ? ждём"


def test_status_without_task():
    assert format_terminal_title(status="completed") == "✦ GigaCode ✓"


def test_unknown_status_falls_back_to_dot():
    # Неизвестное слово статуса → глифа нет, разделитель ·.
    assert format_terminal_title("x", "weird") == "✦ GigaCode · x"


def test_base_and_status_kept_prompt_truncated():
    long = "x" * 200
    out = format_terminal_title(long, status="running", max_len=10)
    assert out.startswith("✦ GigaCode ▸ ")
    assert out.endswith("…")


def test_set_terminal_title_writes_osc():
    buf = io.StringIO()
    set_terminal_title("✦ GigaCode ▸ x", out=buf)
    assert buf.getvalue() == "\033]0;✦ GigaCode ▸ x\007"


def test_set_terminal_title_uses_utf8_buffer_when_present():
    class _Stream:
        def __init__(self):
            self.buffer = io.BytesIO()

    s = _Stream()
    set_terminal_title("✦ GigaCode ✗ x", out=s)
    assert s.buffer.getvalue() == "\033]0;✦ GigaCode ✗ x\007".encode()


def test_set_terminal_title_none_out_is_safe():
    set_terminal_title("x", out=None)
