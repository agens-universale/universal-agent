"""Маркер CUTTED на preview при транспортном capture cap — не в store, без OF N."""
from types import SimpleNamespace

from tools.exec.stream_preview import CAPTURE_CAP_MARKER, process_exec_stream
from tools.session.stored_outputs.tool_call_outputs_store import ToolCallOutputsStore


def _state():
    return SimpleNamespace(tool_call_outputs_store=ToolCallOutputsStore())


def test_capture_cap_appends_cutted_to_preview_not_store():
    state = _state()
    meta = process_exec_stream(
        "short",
        4000,
        hit_capture_cap=True,
        state=state,
        call_id="c1",
        tool_name="exec_command",
    )
    assert meta["text"] == "short" + CAPTURE_CAP_MARKER
    assert "total unknown" in CAPTURE_CAP_MARKER
    assert "OF " not in CAPTURE_CAP_MARKER
    assert meta["truncated"] is False
    record = state.tool_call_outputs_store.get(meta["output_ref"])
    assert record is not None
    assert record.text == "short"
    assert CAPTURE_CAP_MARKER not in record.text


def test_preview_omitted_without_capture_cap_has_no_cutted():
    state = _state()
    captured = "A" * 500
    meta = process_exec_stream(
        captured,
        50,
        hit_capture_cap=False,
        state=state,
        call_id="c1",
        tool_name="exec_command",
    )
    assert meta["truncated"] is True
    assert "[omitted" in meta["text"]
    assert CAPTURE_CAP_MARKER not in meta["text"]
    record = state.tool_call_outputs_store.get(meta["output_ref"])
    assert record.text == captured


def test_capture_cap_and_preview_cut_keep_both_markers_store_clean():
    state = _state()
    captured = "B" * 500
    meta = process_exec_stream(
        captured,
        50,
        hit_capture_cap=True,
        state=state,
        call_id="c1",
        tool_name="exec_command",
    )
    assert "[omitted" in meta["text"]
    assert meta["text"].endswith(CAPTURE_CAP_MARKER)
    record = state.tool_call_outputs_store.get(meta["output_ref"])
    assert record.text == captured
    assert CAPTURE_CAP_MARKER not in record.text
