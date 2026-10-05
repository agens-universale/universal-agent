"""Разбор input: последний элемент, пустой текст, чужие части."""

import pytest
from gc_chat.responses.errors import ApiError
from gc_chat.responses.input_text import parse_turn, turn_text


def test_string_input_is_the_whole_turn():
    incoming = parse_turn({"model": "ui", "input": "привет"})
    assert incoming.text == "привет"
    assert incoming.model == "ui"
    assert incoming.previous_response_id is None
    assert incoming.stream is False


def test_stream_flag_is_only_true():
    assert parse_turn({"model": "m", "input": "a", "stream": True}).stream
    assert not parse_turn(
        {"model": "m", "input": "a", "stream": "true"}
    ).stream


def test_array_uses_only_the_last_element():
    text = turn_text(
        [
            {"role": "user", "content": "старая реплика"},
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": "сырой результат",
            },
            {"type": "input_image", "image_url": "http://img"},
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": "http://img"},
                    {"type": "input_file", "file_id": "f"},
                    {"type": "input_text", "text": "первая"},
                    {"type": "input_text", "text": "вторая"},
                ],
            },
        ]
    )
    assert text == "первая\nвторая"


def test_user_message_type_and_bare_string():
    assert turn_text(
        [{"type": "message", "role": "user", "content": "да"}]
    ) == ("да")
    assert turn_text(["хвост"]) == "хвост"


def test_last_element_without_text_is_empty():
    assert turn_text([{"role": "assistant", "content": "нет"}]) == ""
    assert turn_text([{"type": "function_call", "name": "read_file"}]) == ""
    assert (
        turn_text(
            [{"type": "function_call_output", "call_id": "c", "output": "x"}]
        )
        == ""
    )
    assert turn_text([]) == ""
    assert turn_text("") == ""


def test_empty_text_is_400():
    with pytest.raises(ApiError) as caught:
        parse_turn(
            {
                "model": "m",
                "input": [
                    {"role": "user", "content": "история"},
                    {"role": "assistant", "content": "ответ"},
                ],
            }
        )
    assert caught.value.status == 400
    assert caught.value.code == "invalid_request"


def test_missing_model_and_non_object_are_400():
    with pytest.raises(ApiError) as missing:
        parse_turn({"input": "a"})
    assert missing.value.status == 400
    with pytest.raises(ApiError) as blank:
        parse_turn({"model": "", "input": "a"})
    assert blank.value.status == 400
    with pytest.raises(ApiError) as body:
        parse_turn(["не объект"])
    assert body.value.status == 400


def test_metadata_keeps_string_pairs_only():
    incoming = parse_turn(
        {
            "model": "m",
            "input": "a",
            "metadata": {"cwd": "/tmp/work", "tag": "1"},
        }
    )
    assert incoming.metadata == {"cwd": "/tmp/work", "tag": "1"}
    assert incoming.cwd_sent == "/tmp/work"
    mixed = parse_turn({"model": "m", "input": "a", "metadata": {"n": 1}})
    assert mixed.metadata == {}
    assert mixed.cwd_sent is None


def test_ignored_fields_do_not_reject():
    incoming = parse_turn(
        {
            "model": "m",
            "input": "a",
            "instructions": "нет",
            "tools": [{"type": "function"}],
            "tool_choice": "none",
            "temperature": 0,
            "max_output_tokens": 10,
            "store": False,
            "truncation": "auto",
        }
    )
    assert incoming.text == "a"
