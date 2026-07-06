import pytest

from intent_log import (
    ACTION_FINAL_ANSWER,
    IntentRecord,
    append_final_answer,
    deserialize_list,
    flush,
    serialize_for_meta,
    validate_record,
)


def test_final_answer_record_valid_and_stripped():
    rec = validate_record(
        IntentRecord(
            turn=1,
            ts="2026-01-01T00:00:00Z",
            action=ACTION_FINAL_ANSWER,
            tool_name=None,
            content="  hello  ",
        )
    )
    assert rec.action == "final_answer"
    assert rec.tool_name is None
    assert rec.content == "hello"
    assert rec.fallback is None


def test_final_answer_rejects_tool_name():
    with pytest.raises(ValueError):
        validate_record(
            IntentRecord(
                turn=1,
                ts="2026-01-01T00:00:00Z",
                action=ACTION_FINAL_ANSWER,
                tool_name="x",
                content="a",
            )
        )


def test_final_answer_rejects_empty_content():
    with pytest.raises(ValueError):
        validate_record(
            IntentRecord(
                turn=1,
                ts="2026-01-01T00:00:00Z",
                action=ACTION_FINAL_ANSWER,
                tool_name=None,
                content="   ",
            )
        )


def test_append_final_answer_appends_and_opens_turn():
    out = append_final_answer([], "answer text", user_turn_seq=2)
    assert len(out) == 1
    assert out[0].action == "final_answer"
    assert out[0].turn == 3
    assert out[0].content == "answer text"


def test_append_final_answer_empty_is_noop():
    assert append_final_answer([], "   ", user_turn_seq=0) == []


def test_final_answer_meta_roundtrip():
    recs = append_final_answer([], "final text", user_turn_seq=0)
    back = deserialize_list(serialize_for_meta(recs))
    assert back[0].action == "final_answer"
    assert back[0].content == "final text"


def test_final_answer_flush_moves_to_closed_log():
    current = append_final_answer([], "done", user_turn_seq=0)
    new_current, new_log, seq = flush(current, [], 0)
    assert new_current == []
    assert len(new_log) == 1 and new_log[0].action == "final_answer"
    assert seq == 1
