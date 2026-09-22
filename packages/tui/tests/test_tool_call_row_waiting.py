"""ToolCallRow waiting phase (D9): · without elapsed until execute starts."""

from gc_tui.widgets.tool_call_row import ToolCallRow


def test_waiting_row_shows_dim_dot_without_elapsed():
    row = ToolCallRow(
        call_id="c1",
        tool_name="fs_read_file",
        args_summary="path='a.py'",
        waiting=True,
    )
    assert "·" in str(row._render_prefix())
    content = str(row._render_content())
    assert "fs_read_file" in content
    assert "прервано" not in content
    # нет тикера elapsed
    assert "· 0с" not in content
    assert "· 1с" not in content


def test_set_active_promotes_waiting_to_running():
    row = ToolCallRow(
        call_id="c1",
        tool_name="fs_read_file",
        args_summary="x",
        waiting=True,
    )
    row.set_active()
    assert "▸" in str(row._render_prefix())
    assert "·" in str(row._render_content())  # elapsed · Ns


def test_set_waiting_demotes_running():
    row = ToolCallRow(call_id="c1", tool_name="fs_read_file", args_summary="x")
    assert "▸" in str(row._render_prefix())
    row.set_waiting()
    assert "·" in str(row._render_prefix())
    assert "прервано" not in str(row._render_content())


def test_refresh_elapsed_noop_while_waiting():
    row = ToolCallRow(
        call_id="c1",
        tool_name="t",
        args_summary="x",
        waiting=True,
    )
    row.refresh_running_elapsed()
    assert "· 0с" not in str(row._render_content())


def test_set_waiting_noop_after_result():
    row = ToolCallRow(call_id="c1", tool_name="t", args_summary="x")
    row.set_result(summary="ok", error=False)
    row.set_waiting()
    assert "✓" in str(row._render_prefix())


def test_set_live_status_on_running_row():
    row = ToolCallRow(call_id="c1", tool_name="subagent_worker", args_summary="r")
    row.set_live_status("step 4/100 · last=fs_read_file")
    content = str(row._render_content())
    assert "step 4/100" in content
    assert "last=fs_read_file" in content


def test_reconcile_batch_phases_first_active_rest_waiting():
    from gc_tui.services.subagent_progress import reconcile_batch_phases

    a = ToolCallRow(call_id="a", tool_name="t1", args_summary="", waiting=True)
    b = ToolCallRow(call_id="b", tool_name="t2", args_summary="", waiting=True)
    c = ToolCallRow(call_id="c", tool_name="t3", args_summary="", waiting=True)
    reconcile_batch_phases([a, b, c])
    assert "▸" in str(a._render_prefix())
    assert "·" in str(b._render_prefix()) and "прервано" not in str(b._render_content())
    assert "·" in str(c._render_prefix()) and "прервано" not in str(c._render_content())


def test_reconcile_after_first_done_promotes_second():
    from gc_tui.services.subagent_progress import reconcile_batch_phases

    a = ToolCallRow(call_id="a", tool_name="t1", args_summary="", waiting=True)
    b = ToolCallRow(call_id="b", tool_name="t2", args_summary="", waiting=True)
    reconcile_batch_phases([a, b])
    a.set_result(summary="ok", error=False)
    reconcile_batch_phases([b])  # a already done / removed from active
    assert "▸" in str(b._render_prefix())
