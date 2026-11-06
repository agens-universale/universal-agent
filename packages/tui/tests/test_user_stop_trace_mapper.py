"""TUI: user-stop statuses → error row, not success."""

from __future__ import annotations

import json

from gc_tui.services.trace_mapper import _looks_like_error, _summarize_output


def test_cancelled_by_user_looks_like_error():
    out = json.dumps(
        {
            "tool": "subagent_worker",
            "ok": False,
            "status": "cancelled_by_user",
            "message": "Tool was interrupted by user stop.",
            "error": {
                "code": "cancelled_by_user",
                "message": "x",
                "retryable": True,
            },
        }
    )
    assert _looks_like_error(out) is True
    assert _summarize_output(out, error=True) == "отменено"


def test_not_started_due_to_user_stop_looks_like_error():
    out = json.dumps(
        {
            "tool": "fs_read_file",
            "ok": False,
            "status": "not_started_due_to_user_stop",
            "message": "Tool was not started due to manual user stop.",
            "error": None,
            "synthetic": True,
        }
    )
    assert _looks_like_error(out) is True
    assert _summarize_output(out, error=True) == "не запущено (стоп)"


def test_not_started_due_to_session_compact_looks_like_error():
    out = json.dumps(
        {
            "tool": "session_think",
            "ok": False,
            "status": "not_started_due_to_session_compact",
            "message": "Tool was not started: session_compact replaced the history.",
            "error": None,
            "synthetic": True,
        }
    )
    assert _looks_like_error(out) is True
    assert (
        _summarize_output(out, error=True)
        == "не исполнен: сводка заменила историю"
    )
