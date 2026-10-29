"""Служебные строки harness дописываются в output инструмента."""

import json

from gc_core.message_policy.nudges import (
    NudgeBuffer,
    attach_technical_system_messages,
    output_for_card,
)


def test_json_object_gains_the_list_and_keeps_other_keys():
    raw = json.dumps(
        {"ok": False, "message": "denied", "tool": "fs_read"},
        ensure_ascii=False,
    )
    parsed = json.loads(
        attach_technical_system_messages(raw, ["Loop detector: stop"])
    )
    assert parsed["ok"] is False
    assert parsed["message"] == "denied"
    assert parsed["tool"] == "fs_read"
    assert parsed["technical_system_messages"] == ["Loop detector: stop"]
    assert set(parsed) == {
        "ok",
        "message",
        "tool",
        "technical_system_messages",
    }


def test_empty_list_leaves_the_string_unchanged():
    raw = '{"ok": true, "content": "a"}'
    assert attach_technical_system_messages(raw, []) == raw
    assert attach_technical_system_messages(raw, ["", ""]) == raw


def test_non_object_is_wrapped_without_adding_ok():
    wrapped = json.loads(
        attach_technical_system_messages("plain text", ["hint"])
    )
    assert wrapped == {
        "result": "plain text",
        "technical_system_messages": ["hint"],
    }
    assert "ok" not in wrapped

    array = json.loads(attach_technical_system_messages("[1, 2]", ["hint"]))
    assert array["result"] == "[1, 2]"

    number = json.loads(attach_technical_system_messages("42", ["hint"]))
    assert number["result"] == "42"

    broken = json.loads(attach_technical_system_messages("{", ["hint"]))
    assert broken["result"] == "{"

    empty = json.loads(attach_technical_system_messages("", ["hint"]))
    assert empty["result"] == ""


def test_non_object_without_hints_stays_as_it_was():
    assert attach_technical_system_messages("plain", []) == "plain"
    assert attach_technical_system_messages("[1]", []) == "[1]"
    assert attach_technical_system_messages("", []) == ""


def test_occupied_key_uses_the_next_free_suffix():
    raw = json.dumps(
        {
            "ok": True,
            "technical_system_messages": ["foreign"],
            "technical_system_messages_1": ["also"],
        }
    )
    parsed = json.loads(attach_technical_system_messages(raw, ["ours"]))
    assert parsed["technical_system_messages"] == ["foreign"]
    assert parsed["technical_system_messages_1"] == ["also"]
    assert parsed["technical_system_messages_2"] == ["ours"]
    assert parsed["ok"] is True
    assert "error" not in parsed


def test_stored_envelope_keeps_the_list_beside_content():
    raw = json.dumps(
        {
            "type": "stored_tool_output",
            "ok": True,
            "content": "page-one",
            "output_ref": "out_1",
            "offset": 0,
            "next_offset": 8,
        }
    )
    parsed = json.loads(
        attach_technical_system_messages(raw, ["loop", "mode"])
    )
    assert parsed["content"] == "page-one"
    assert parsed["output_ref"] == "out_1"
    assert parsed["offset"] == 0
    assert parsed["next_offset"] == 8
    assert parsed["technical_system_messages"] == ["loop", "mode"]
    assert "technical_system_messages" not in parsed["content"]


def test_card_hides_harness_keys_and_keeps_ok():
    raw = json.dumps(
        {
            "ok": False,
            "message": "denied",
            "technical_system_messages": ["Loop detector: again"],
            "technical_system_messages_2": ["mode"],
        },
        ensure_ascii=False,
    )
    shown = json.loads(output_for_card(raw))
    assert shown == {"ok": False, "message": "denied"}
    assert "Loop detector" not in output_for_card(raw)


def test_card_shows_original_text_of_a_wrapped_non_object():
    raw = attach_technical_system_messages("plain text", ["hint"])
    assert output_for_card(raw) == "plain text"


def test_card_leaves_a_body_without_harness_keys_unchanged():
    raw = '{"ok": true, "content": "a"}'
    assert output_for_card(raw) == raw
    assert output_for_card("not json") == "not json"


def test_nudge_buffer_flush_and_clear():
    buf = NudgeBuffer()
    buf.emit("  loop  ")
    buf.extend(["", "mode", "loop"])
    assert bool(buf)
    carrier = {"output": json.dumps({"ok": True})}
    buf.flush_into(carrier)
    body = json.loads(carrier["output"])
    assert body["ok"] is True
    assert body["technical_system_messages"] == ["loop", "mode", "loop"]
    assert not buf
    buf.emit("x")
    buf.clear()
    assert not buf
    assert buf.take() == []
