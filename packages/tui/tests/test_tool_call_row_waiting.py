"""ToolCallRow waiting phase (D9): • without elapsed until execute starts."""

from gc_tui.palette import DIM, RUN_COLOR
from gc_tui.widgets.tool_call_row import ToolCallRow


def _glyph_style(row: ToolCallRow) -> tuple[str, str]:
    prefix = row._render_prefix()
    (span,) = prefix.spans[-1:]
    return prefix.plain[span.start : span.end], str(span.style)


def test_waiting_glyph_is_dim_bullet():
    row = ToolCallRow(call_id="c1", label="t", target="x", waiting=True)
    assert _glyph_style(row) == ("•", DIM)


def test_running_glyph_is_amber_circle():
    row = ToolCallRow(call_id="c1", label="t", target="x")
    assert _glyph_style(row) == ("●", RUN_COLOR)


def test_interrupted_glyph_is_grey_hollow_circle():
    row = ToolCallRow(call_id="c1", label="t", target="x")
    row.mark_interrupted()
    assert _glyph_style(row) == ("○", DIM)


def test_waiting_row_shows_dim_dot_without_elapsed():
    row = ToolCallRow(
        call_id="c1",
        label="fs_read_file",
        target="path='a.py'",
        waiting=True,
    )
    assert "•" in str(row._render_prefix())
    content = str(row._render_content())
    assert "fs_read_file" in content
    assert "прервано" not in content
    # нет тикера elapsed
    assert "· 0с" not in content
    assert "· 1с" not in content


def test_set_active_promotes_waiting_to_running():
    row = ToolCallRow(
        call_id="c1",
        label="fs_read_file",
        target="x",
        waiting=True,
    )
    row.set_active()
    assert "●" in str(row._render_prefix())
    row._start_monotonic -= 1  # живой таймер — с целой секунды
    assert "· 1с" in str(row._render_content())


def test_refresh_elapsed_noop_while_waiting():
    row = ToolCallRow(
        call_id="c1",
        label="t",
        target="x",
        waiting=True,
    )
    row.refresh_running_elapsed()
    assert "· 0с" not in str(row._render_content())


def test_set_live_status_on_running_row():
    row = ToolCallRow(call_id="c1", label="subagent_worker", target="r")
    row.set_live_status("step 4/100 · last=fs_read_file")
    content = str(row._render_content())
    assert "step 4/100" in content
    assert "last=fs_read_file" in content


def test_first_pending_is_first_unfinished():
    """Батч исполняется по порядку: стартовала первая незавершённая
    строка; сама функция строк не меняет (фазы — через модель блоков)."""
    from gc_tui.services.subagent_progress import first_pending

    a = ToolCallRow(call_id="a", label="t1", target="", waiting=True)
    b = ToolCallRow(call_id="b", label="t2", target="", waiting=True)
    assert first_pending([a, b]) is a
    assert "•" in str(a._render_prefix())
    a.set_result(summary="ok", outcome="ok")
    assert first_pending([a, b]) is b
    b.mark_interrupted()
    assert first_pending([a, b]) is None
