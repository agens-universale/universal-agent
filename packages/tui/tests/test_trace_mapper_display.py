from gc_tui.services.trace_mapper import map_message


def test_user_event_uses_display_content():
    msg = {
        "role": "user",
        "content": "<skill>\nдавай обсудим",
        "display_content": "давай обсудим",
    }
    ev = map_message(msg, no=1, ts="")
    assert ev.kind == "user"
    assert ev.summary == "давай обсудим"
    assert "skill" not in ev.summary.lower()


def test_user_event_empty_display_is_skipped():
    msg = {"role": "user", "content": "<skill>", "display_content": ""}
    ev = map_message(msg, no=1, ts="")
    assert ev is None  # mid-run доставка — не рисуем реплику


def test_user_event_without_display_unchanged():
    ev = map_message({"role": "user", "content": "просто запрос"}, no=1, ts="")
    assert ev.kind == "user" and ev.summary == "просто запрос"
