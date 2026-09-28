from gc_tui.services.trace_mapper import map_message


def test_reasoning_message_maps_to_none():
    ev = map_message(
        {"type": "reasoning", "content": "длинное рассуждение"},
        no=1,
        ts="2026-06-16T00:00:00+00:00",
    )
    assert ev is None


def test_think_message_maps_to_none():
    ev = map_message(
        {"type": "think", "summary": "рассуждение"},
        no=1,
        ts="2026-06-16T00:00:00+00:00",
    )
    assert ev is None
