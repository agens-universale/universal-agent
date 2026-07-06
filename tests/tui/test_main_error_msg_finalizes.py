# tests/tui/test_main_error_msg_finalizes.py
# Юнит на ветку рендера: подменяем _active_calls фейковой строкой и проверяем,
# что error_msg с известным call_id финализирует её, а отдельная [!]-строка не плодится.
from tui.models import TraceEvent
from tui.screens.main import TraceScreen

class _FakeRow:
    def __init__(self): self.finalized = None
    def set_result(self, *, summary, error): self.finalized = (summary, error)

def _ev(call_id):
    return TraceEvent(no=1, event_ts_utc_iso="", kind="error_msg",
                      title="!", summary="Wrong arguments for `x`",
                      full_payload={}, call_id=call_id)

def test_error_msg_with_call_id_finalizes_row():
    screen = TraceScreen.__new__(TraceScreen)
    row = _FakeRow()
    screen._active_calls = {"call_7": row}
    mounted = []
    screen._mount_trace_row = lambda **kw: mounted.append(kw)
    screen._render_error_msg(_ev("call_7"))
    assert row.finalized == ("Wrong arguments for `x`", True)
    assert mounted == []            # [!]-строка подавлена
    assert "call_7" not in screen._active_calls

def test_error_msg_without_match_falls_back():
    screen = TraceScreen.__new__(TraceScreen)
    screen._active_calls = {}
    mounted = []
    screen._mount_trace_row = lambda **kw: mounted.append(kw)
    screen._render_error_msg(_ev(None))
    assert len(mounted) == 1        # fallback [!]-строка
    assert mounted[0]["prefix"] == "[!] "
