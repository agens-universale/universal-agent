# tests/tui/test_main_error_msg_finalizes.py
# Юнит на ветку рендера: подменяем _active_calls фейковой строкой и проверяем,
# что error_msg с известным call_id финализирует её, а строка `!` не плодится.
from gc_tui.models import TraceEvent
from gc_tui.screens.main import TraceScreen


class _FakeRow:
    def __init__(self):
        self.finalized = None

    def set_result(self, *, summary, outcome):
        self.finalized = (summary, outcome)


def _ev(call_id):
    return TraceEvent(
        no=1,
        event_ts_utc_iso="",
        kind="error_msg",
        title="!",
        summary="Wrong arguments for `x`",
        full_payload={},
        call_id=call_id,
    )


def test_error_msg_with_call_id_finalizes_row():
    screen = TraceScreen.__new__(TraceScreen)
    row = _FakeRow()
    screen._active_calls = {"call_7": row}
    mounted = []
    screen._trace_mount = lambda w, **kw: mounted.append(w)
    screen._render_error_msg(_ev("call_7"))
    assert row.finalized == ("Wrong arguments for `x`", "error")
    assert mounted == []  # строка `!` подавлена
    assert "call_7" not in screen._active_calls


def test_error_msg_without_match_falls_back():
    screen = TraceScreen.__new__(TraceScreen)
    screen._active_calls = {}
    mounted = []
    screen._trace_mount = lambda w, **kw: mounted.append(w)
    screen._render_error_msg(_ev(None))
    # Ошибка без владельца — `!` в колонке `●`, ERR, без квадратных скобок.
    (row,) = mounted
    assert (row._prefix, row._content) == ("! ", "Wrong arguments for `x`")
    assert row.has_class("line-error")
