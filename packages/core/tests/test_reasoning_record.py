"""Запись «мысль модели» в файле треда: схема v1 и её разбор."""

from gc_core.persistence.reasoning_record import (
    THOUGHT_TYPE,
    ReasoningRecord,
    is_reasoning_record,
    parse_reasoning,
    reasoning_entry,
)


def test_reasoning_entry_roundtrip():
    rec = ReasoningRecord(text="план", duration_sec=2.5, attempt="att_1")
    env = {"type": THOUGHT_TYPE, **reasoning_entry(rec, after="msg_1")}

    assert env["after"] == "msg_1"
    assert env["thought"] == {
        "v": 1,
        "text": "план",
        "duration_sec": 2.5,
        "attempt": "att_1",
    }
    assert is_reasoning_record(env)
    assert parse_reasoning(env) == rec


def test_roundtrip_without_duration_and_attempt():
    rec = ReasoningRecord(text="план", duration_sec=None, attempt=None)
    env = {"type": THOUGHT_TYPE, **reasoning_entry(rec, after=None)}

    assert parse_reasoning(env) == rec


def test_parse_rejects_empty_text_and_foreign():
    bad = {"type": THOUGHT_TYPE, "thought": {"v": 1, "text": ""}}

    assert parse_reasoning(bad) is None
    assert parse_reasoning({"type": "model_choice"}) is None
    assert not is_reasoning_record({"type": "reasoning"})


def test_parse_rejects_unknown_version_and_bad_types():
    for payload in (
        {"v": 2, "text": "x"},
        {"v": 1, "text": 5},
        {"v": 1, "text": "x", "duration_sec": "1"},
        {"v": 1, "text": "x", "attempt": 3},
    ):
        env = {"type": THOUGHT_TYPE, "thought": payload}
        assert parse_reasoning(env) is None
