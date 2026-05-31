import json
from types import SimpleNamespace

import pytest

from config_modules.intent_scope import DEFAULT_INTENT_LOG_READ_PAGE_LIMIT
from intent_log import (
    ACTION_TOOL_AGGREGATED,
    ACTION_USER_ASK,
    IntentRecord,
    append_tool_aggregated,
    append_user_ask,
    flush,
    read_page,
    serialize,
)
from tools.read_user_intent_log import ReadUserIntentLog
from tools.simple_tools.__init__ import get_simple_tools_data


FIXED_TS = "2026-05-30T11:35:08Z"


def _seed_closed_log(state, *contents: str) -> None:
    state.user_turn_seq = 0
    state.user_intent_log = []
    state.current_user_intent = []
    for text in contents:
        state.current_user_intent = append_user_ask(
            state.current_user_intent,
            text,
            user_turn_seq=state.user_turn_seq,
            ts=FIXED_TS,
        )
        state.current_user_intent, state.user_intent_log, state.user_turn_seq = flush(
            state.current_user_intent,
            state.user_intent_log,
            state.user_turn_seq,
        )


def _record(content: str, *, turn: int = 1) -> IntentRecord:
    return IntentRecord(
        turn=turn,
        ts=FIXED_TS,
        action=ACTION_USER_ASK,
        tool_name=None,
        content=content,
    )


def _make_state(*, log=None, current=None):
    state = SimpleNamespace()
    state.user_turn_seq = 0
    state.user_intent_log = list(log or [])
    state.current_user_intent = list(current or [])
    return state


def _call(state, **kwargs):
    ctx = SimpleNamespace(config_dict={"backend_api_mode": "chat_completions"})
    tool = ReadUserIntentLog(ctx=ctx, state=state, config_dict=ctx.config_dict)
    return json.loads(tool(**kwargs))


def test_info_reports_closed_log_totals():
    records = [_record("hello"), _record("world", turn=2)]
    state = _make_state(log=records)

    res = _call(state, action="info")

    assert res["ok"] is True
    assert res["action"] == "info"
    assert res["total_records"] == 2
    assert res["total_chars"] == len(serialize(records))
    assert res["default_limit"] == DEFAULT_INTENT_LOG_READ_PAGE_LIMIT
    assert res["error"] is None


def test_read_pagination_matches_read_page():
    long_text = "x" * 30
    records = [_record(long_text), _record("tail", turn=2)]
    state = _make_state(log=records)
    page = read_page(records, offset=5, limit=10, default_limit=DEFAULT_INTENT_LOG_READ_PAGE_LIMIT)

    res = _call(state, action="read", offset=5, limit=10)

    assert res["ok"] is True
    assert res["action"] == "read"
    assert res["offset"] == page["offset"]
    assert res["limit"] == page["limit"]
    assert res["content"] == page["content"]
    assert res["next_offset"] == page["next_offset"]
    assert res["truncated"] == page["truncated"]
    assert res["total_chars"] == page["total_chars"]
    assert res["total_records"] == page["total_records"]


def test_read_default_limit_from_config():
    records = [_record("short")]
    state = _make_state(log=records)
    ctx = SimpleNamespace(
        config_dict={
            "backend_api_mode": "chat_completions",
            "intent_scope_params": {"read_page_limit": 123},
        }
    )
    tool = ReadUserIntentLog(ctx=ctx, state=state, config_dict=ctx.config_dict)
    res = json.loads(tool(action="read", offset=0))

    assert res["ok"] is True
    assert res["default_limit"] == 123
    assert res["limit"] == 123


def test_read_offset_zero_after_long_thread():
    contents = [f"turn-{i}-" + ("z" * 50) for i in range(25)]
    state = SimpleNamespace()
    _seed_closed_log(state, *contents)
    full_log = list(state.user_intent_log)

    res = _call(state, action="read", offset=0, limit=5000)

    assert res["ok"] is True
    assert res["offset"] == 0
    assert res["content"] == serialize(full_log)[:5000]
    assert res["total_records"] == len(full_log)
    assert res["truncated"] is (len(serialize(full_log)) > 5000)


def test_excludes_current_user_intent():
    state = _make_state(log=[_record("closed")], current=[_record("open")])
    res = _call(state, action="info")
    assert res["total_records"] == 1
    assert "open" not in serialize(state.user_intent_log)


def test_invalid_action_and_missing_state():
    res = _call(_make_state(), action="write")
    assert res["ok"] is False
    assert res["error"]["type"] == "invalid_action"

    tool = ReadUserIntentLog(state=None)
    res = json.loads(tool(action="info"))
    assert res["ok"] is False
    assert res["error"]["type"] == "no_state"


def test_registered_only_for_chat_completions():
    assert "read_user_intent_log" not in get_simple_tools_data({"backend_api_mode": "responses"})
    tools = get_simple_tools_data({"backend_api_mode": "chat_completions"})
    assert "read_user_intent_log" in tools
    assert tools["read_user_intent_log"]["tool_fabric"] is ReadUserIntentLog


def test_render_for_finish_gate_round_trip_in_guard_path():
    from intent_log import render_for_finish_gate

    current = append_user_ask([], "open ask", user_turn_seq=1, ts=FIXED_TS)
    current = append_tool_aggregated(
        current,
        "ask_human",
        "picked topic",
        user_turn_seq=1,
        ts=FIXED_TS,
    )
    closed, log, _ = flush(current, [], user_turn_seq=1)

    lines = render_for_finish_gate(log)
    assert lines == ["open ask", "[ask_human] picked topic"]
    assert render_for_finish_gate([]) == []
