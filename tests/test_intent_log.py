import json

import pytest

from config_modules.intent_scope import DEFAULT_INTENT_LOG_READ_PAGE_RECORD_LIMIT
from intent_log import (
    ACTION_TOOL_AGGREGATED,
    ACTION_TOOL_RAW,
    ACTION_USER_ASK,
    IntentRecord,
    append_tool_aggregated,
    append_tool_raw,
    append_user_ask,
    deserialize_list,
    flush,
    read_info,
    read_page,
    serialize,
    tail_preview,
    validate_record,
)


FIXED_TS = "2026-05-30T11:35:08Z"


def _user_ask(text: str, *, turn: int = 1) -> IntentRecord:
    return IntentRecord(
        turn=turn,
        ts=FIXED_TS,
        action=ACTION_USER_ASK,
        tool_name=None,
        content=text,
    )


def test_validate_record_accepts_structured_entries():
    record = validate_record(
        {
            "turn": 2,
            "ts": FIXED_TS,
            "action": ACTION_TOOL_AGGREGATED,
            "tool_name": "ask_human",
            "content": "summary line",
            "fallback": True,
        }
    )
    assert record.action == ACTION_TOOL_AGGREGATED
    assert record.fallback is True


def test_validate_record_rejects_invalid_action():
    with pytest.raises(ValueError, match="Invalid intent action"):
        validate_record(
            {
                "turn": 1,
                "ts": FIXED_TS,
                "action": "unknown",
                "tool_name": None,
                "content": "x",
            }
        )


def test_validate_record_enforces_content_shape():
    with pytest.raises(ValueError, match="user_ask content"):
        validate_record(
            {
                "turn": 1,
                "ts": FIXED_TS,
                "action": ACTION_USER_ASK,
                "tool_name": None,
                "content": "   ",
            }
        )
    with pytest.raises(ValueError, match="tool_result_raw content"):
        validate_record(
            {
                "turn": 1,
                "ts": FIXED_TS,
                "action": ACTION_TOOL_RAW,
                "tool_name": "submit_solution",
                "content": "not-an-object",
            }
        )


def test_serialize_and_deserialize_list_round_trip():
    records = [
        _user_ask("first question"),
        IntentRecord(
            turn=1,
            ts=FIXED_TS,
            action=ACTION_TOOL_RAW,
            tool_name="submit_solution",
            content={"arguments": {"solution_type": "full_solution"}},
        ),
    ]

    text = serialize(records)
    assert text.count("\n") == 2
    assert json.loads(text.splitlines()[0])["action"] == ACTION_USER_ASK

    restored = deserialize_list([r.to_dict() for r in records])
    assert restored == records


def test_append_user_ask_uses_open_turn_and_skips_empty():
    current = append_user_ask([], "  hello  ", user_turn_seq=0, ts=FIXED_TS)
    assert len(current) == 1
    assert current[0].turn == 1
    assert current[0].content == "hello"

    unchanged = append_user_ask(current, "   ", user_turn_seq=0, ts=FIXED_TS)
    assert unchanged == current


def test_append_tool_variants_use_same_open_turn():
    current: list[IntentRecord] = []
    current = append_user_ask(current, "q", user_turn_seq=2, ts=FIXED_TS)
    current = append_tool_aggregated(
        current,
        "ask_human",
        "picked topic",
        user_turn_seq=2,
        ts=FIXED_TS,
    )
    current = append_tool_raw(
        current,
        "submit_solution",
        {"solution_type": "full_solution"},
        output={"ok": True},
        user_turn_seq=2,
        ts=FIXED_TS,
    )

    assert [r.turn for r in current] == [3, 3, 3]
    assert current[1].action == ACTION_TOOL_AGGREGATED
    assert current[2].content == {
        "arguments": {"solution_type": "full_solution"},
        "output": {"ok": True},
    }


def test_flush_moves_current_to_log_and_increments_seq():
    current = append_user_ask([], "turn one", user_turn_seq=0, ts=FIXED_TS)
    log: list[IntentRecord] = []

    new_current, new_log, seq = flush(current, log, user_turn_seq=0)

    assert new_current == []
    assert len(new_log) == 1
    assert new_log[0].content == "turn one"
    assert seq == 1


def test_read_page_slices_by_record_offset():
    records = [
        _user_ask("aaaa"),
        _user_ask("bbbb"),
        _user_ask("cccc", turn=2),
    ]
    page = read_page(
        records,
        offset=1,
        limit=1,
        default_limit=DEFAULT_INTENT_LOG_READ_PAGE_RECORD_LIMIT,
    )

    assert page["total_records"] == 3
    assert page["offset"] == 1
    assert page["limit"] == 1
    assert page["page_records"] == 1
    assert page["content"] == serialize([records[1]])
    assert page["next_offset"] == 2
    assert page["truncated"] is True
    lines = page["content"].strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["content"] == "bbbb"


def test_read_page_returns_valid_jsonl_for_every_page():
    records = [_user_ask(f"line-{i}") for i in range(5)]
    offset = 0
    while offset < len(records):
        page = read_page(
            records,
            offset,
            2,
            default_limit=DEFAULT_INTENT_LOG_READ_PAGE_RECORD_LIMIT,
        )
        if not page["content"]:
            break
        for line in page["content"].strip().splitlines():
            json.loads(line)
        offset = page["next_offset"]


def test_read_info_reports_totals():
    records = [_user_ask("hello")]
    info = read_info(records, default_limit=DEFAULT_INTENT_LOG_READ_PAGE_RECORD_LIMIT)

    assert info["total_records"] == 1
    assert info["total_chars"] == len(serialize(records))
    assert info["default_limit"] == DEFAULT_INTENT_LOG_READ_PAGE_RECORD_LIMIT


def test_tail_preview_returns_suffix_records_within_char_budget():
    records = [_user_ask("x" * 10), _user_ask("y" * 10, turn=2)]
    preview = tail_preview(records, max_chars=10_000)

    assert preview["truncated"] is False
    assert preview["offset"] == 0
    assert preview["limit"] == 2
    assert preview["content"] == serialize(records)
    for line in preview["content"].strip().splitlines():
        json.loads(line)


def test_tail_preview_truncates_by_records_not_mid_line():
    records = [_user_ask("x" * 5), _user_ask("y" * 30, turn=2)]
    preview = tail_preview(records, max_chars=50)

    assert preview["truncated"] is True
    assert preview["offset"] == 1
    assert preview["limit"] == 1
    assert preview["content"] == serialize([records[1]])
    assert json.loads(preview["content"].strip())["content"] == "y" * 30


def test_tail_preview_includes_oversized_single_record_whole():
    records = [_user_ask("x" * 5), _user_ask("y" * 200, turn=2)]
    preview = tail_preview(records, max_chars=20)

    assert preview["offset"] == 1
    assert preview["limit"] == 1
    assert preview["content"] == serialize([records[1]])


def test_tail_preview_returns_full_log_when_short():
    records = [_user_ask("short")]
    preview = tail_preview(records, max_chars=1000)

    assert preview["truncated"] is False
    assert preview["offset"] == 0
    assert preview["limit"] == 1
    assert preview["content"] == serialize(records)


def test_records_to_jsonl_lines_emits_canonical_wire():
    import json

    from intent_log import deserialize_list, records_to_jsonl_lines

    records = [
        _user_ask("user line"),
        IntentRecord(
            turn=1,
            ts=FIXED_TS,
            action=ACTION_TOOL_AGGREGATED,
            tool_name="ask_human",
            content="summary",
        ),
    ]
    lines = records_to_jsonl_lines(records)
    assert len(lines) == 2
    assert json.loads(lines[0])["action"] == ACTION_USER_ASK
    assert json.loads(lines[0])["content"] == "user line"
    assert json.loads(lines[1])["action"] == ACTION_TOOL_AGGREGATED
    assert json.loads(lines[1])["tool_name"] == "ask_human"

    legacy_ask = records_to_jsonl_lines(deserialize_list(["legacy plain ask"]))
    assert len(legacy_ask) == 1
    assert json.loads(legacy_ask[0])["action"] == ACTION_USER_ASK
    assert json.loads(legacy_ask[0])["content"] == "legacy plain ask"
