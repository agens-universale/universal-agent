"""kind ядра → события SSE."""

from gc_server.chat.responses.stream_map import StreamMap, unseen_answer


def _map() -> StreamMap:
    return StreamMap(
        response_id="resp_1",
        model="ui-model",
        previous_response_id=None,
        metadata={"cwd": "/work"},
        created_at=10,
    )


def _types(events: list[dict]) -> list[str]:
    return [event["type"] for event in events]


def test_opening_events_are_in_progress_with_closed_output():
    events = _map().opening_events()
    assert _types(events) == ["response.created", "response.in_progress"]
    for event in events:
        assert event["response"]["status"] == "in_progress"
        assert event["response"]["output"] == []
        assert event["response"]["usage"] is None


def test_answer_opens_message_and_keeps_the_chunk():
    events = _map().feed("answer", " Hello ")
    assert _types(events) == [
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
    ]
    assert events[-1]["delta"] == " Hello "
    assert events[0]["item"]["content"][0]["text"] == ""


def test_empty_chunk_and_tool_kind_are_silent():
    mapper = _map()
    assert mapper.feed("answer", "") == []
    assert mapper.feed("tool", '{"path": "secret"}') == []
    assert mapper.feed("think", "") == []
    sealed = mapper.seal_events(
        "completed", error=None, incomplete_details=None
    )
    assert sealed[-1]["response"]["output"] == []


def test_think_then_answer_closes_reasoning_first():
    mapper = _map()
    mapper.feed("think", "шаг")
    events = mapper.feed("answer", "итог")
    assert _types(events)[:3] == [
        "response.reasoning_summary_text.done",
        "response.reasoning_summary_part.done",
        "response.output_item.done",
    ]
    assert "response.output_item.added" in _types(events)
    assert events[-1]["type"] == "response.output_text.delta"
    assert events[-1]["delta"] == "итог"


def test_reset_drops_the_open_element():
    mapper = _map()
    mapper.feed("answer", "черновик")
    reset = mapper.feed("reset", "")
    assert reset[0]["type"] == "response.gc_stream_reset"
    assert reset[0]["item_id"].startswith("msg_")
    tail = mapper.flush_unseen("чисто")
    assert tail[-1]["delta"] == "чисто"
    final = mapper.seal_events(
        "completed", error=None, incomplete_details=None
    )[-1]["response"]
    assert [item["type"] for item in final["output"]] == ["message"]
    assert final["output"][0]["content"][0]["text"] == "чисто"
    assert "черновик" not in final["output"][0]["content"][0]["text"]


def test_unseen_answer_does_not_repeat_the_prefix():
    assert unseen_answer("Hel", "Hello") == "lo"
    assert unseen_answer("Hello", "Hello") == ""
    assert unseen_answer("Hello!", "Hello") == ""
    assert unseen_answer("", "") == ""
    mapper = _map()
    mapper.feed("answer", "Hel")
    events = mapper.flush_unseen("Hello")
    assert events[-1]["delta"] == "lo"


def test_tool_card_sits_between_messages_without_bodies():
    mapper = _map()
    mapper.feed("answer", "до")
    opened = mapper.apply_tool(
        {
            "op": "open",
            "call_id": "c1",
            "name": "read_file",
            "title": "read_file(path='src/app.py')",
        }
    )
    assert opened[-1]["type"] == "response.output_item.added"
    card = opened[-1]["item"]
    assert card["status"] == "in_progress"
    assert card["type"] == "gc_tool_call"
    assert card["id"] == "gtc_c1"
    assert "arguments" not in card
    assert "output" not in card
    closed = mapper.apply_tool(
        {"op": "close", "call_id": "c1", "status": "failed"}
    )
    assert closed[0]["item"]["status"] == "failed"
    assert "output" not in closed[0]["item"]
    mapper.feed("answer", "после")
    final = mapper.seal_events(
        "completed", error=None, incomplete_details=None
    )[-1]["response"]
    assert [item["type"] for item in final["output"]] == [
        "message",
        "gc_tool_call",
        "message",
    ]
    assert final["output"][1]["status"] == "failed"
    assert "parent_call_id" not in final["output"][1]
    assert final["status"] == "completed"
    assert final["model"] == "ui-model"


def test_nested_tool_card_keeps_the_parent_call_id():
    mapper = _map()
    opened = mapper.apply_tool(
        {
            "op": "open",
            "call_id": "sub/c1",
            "parent_call_id": "sub",
            "name": "read_file",
            "title": "read_file",
        }
    )
    card = opened[-1]["item"]
    assert card["call_id"] == "sub/c1"
    assert card["parent_call_id"] == "sub"
    assert card["id"] == "gtc_sub/c1"
    closed = mapper.apply_tool(
        {"op": "close", "call_id": "sub/c1", "status": "completed"}
    )
    assert closed[0]["item"]["status"] == "completed"
    assert closed[0]["item"]["parent_call_id"] == "sub"


def test_terminal_event_names():
    mapper = _map()
    failed = mapper.seal_events(
        "failed",
        error={"message": "boom", "code": "server_error"},
        incomplete_details=None,
    )
    body = failed[-1]["response"]
    assert failed[-1]["type"] == "response.failed"
    assert body["error"] == {"message": "boom", "code": "server_error"}
    assert body["incomplete_details"] is None
