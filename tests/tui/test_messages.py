"""Tui message classes — несут call_id для роутинга к detail screen."""


def test_open_tool_call_detail_request_carries_call_id():
    from tui.messages import OpenToolCallDetailRequest

    msg = OpenToolCallDetailRequest("fc_42")
    assert msg.call_id == "fc_42"


def test_tool_call_focus_changed_carries_call_id_and_focus():
    from tui.messages import ToolCallFocusChanged

    on = ToolCallFocusChanged("fc_42", focused=True)
    off = ToolCallFocusChanged("fc_42", focused=False)
    assert on.call_id == "fc_42" and on.focused is True
    assert off.call_id == "fc_42" and off.focused is False
