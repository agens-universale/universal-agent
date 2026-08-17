from gc_tui.services.trace_mapper import map_message


def test_error_msg_carries_call_id():
    msg = {
        "role": "user",
        "content": '<technical type="error_message">❌ Wrong arguments for `x`</technical>',
        "id": "m1",
        "call_id": "call_99",
    }
    ev = map_message(msg, no=5, ts="2026-06-30T00:00:00Z")
    assert ev.kind == "error_msg"
    assert ev.call_id == "call_99"


def test_error_msg_without_call_id_is_none():
    msg = {"role": "user",
           "content": '<technical type="error_message">boom</technical>', "id": "m2"}
    ev = map_message(msg, no=6, ts="2026-06-30T00:00:00Z")
    assert ev.kind == "error_msg"
    assert ev.call_id is None
