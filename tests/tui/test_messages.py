"""Tui message classes — несут call_id для роутинга к detail screen."""


def test_open_tool_call_detail_request_carries_call_id():
    from tui.messages import OpenToolCallDetailRequest

    msg = OpenToolCallDetailRequest("fc_42")
    assert msg.call_id == "fc_42"
