"""Вход: ``<think>`` вырезается, последний элемент — user."""

import pytest
from gc_server.chat.errors import ApiError
from gc_server.chat.completions.input_text import parse_turn


def test_think_is_stripped_and_last_user_is_the_prompt():
    turn = parse_turn(
        {
            "model": "ui",
            "messages": [
                {"role": "user", "content": "вопрос"},
                {
                    "role": "assistant",
                    "content": "<think>карточка read_file</think>\nитог",
                },
                {"role": "user", "content": "дальше"},
            ],
        }
    )
    assert turn.prompt == "дальше"
    assert turn.prior == (("user", "вопрос"), ("assistant", "итог"))


def test_unclosed_think_drops_the_assistant_turn():
    turn = parse_turn(
        {
            "model": "ui",
            "messages": [
                {"role": "user", "content": "первый"},
                {"role": "assistant", "content": "<think>оборвалось"},
                {"role": "user", "content": "второй"},
            ],
        }
    )
    assert turn.prior == (("user", "первый"),)
    assert turn.prompt == "второй"


def test_system_tool_calls_and_images_are_skipped():
    turn = parse_turn(
        {
            "model": "ui",
            "messages": [
                {"role": "system", "content": "ты агент"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "посмотри"},
                        {"type": "image_url", "image_url": {"url": "x"}},
                    ],
                },
                {
                    "role": "assistant",
                    "content": "молчу",
                    "tool_calls": [{"id": "c1"}],
                },
                {"role": "tool", "content": "тело"},
                {"role": "user", "content": "ну?"},
            ],
        }
    )
    assert turn.prompt == "ну?"
    assert turn.prior == (("user", "посмотри"),)


def test_last_message_must_be_user_text():
    with pytest.raises(ApiError) as empty:
        parse_turn({"model": "ui", "messages": []})
    assert empty.value.status == 400
    with pytest.raises(ApiError):
        parse_turn(
            {
                "model": "ui",
                "messages": [{"role": "assistant", "content": "один"}],
            }
        )
    with pytest.raises(ApiError):
        parse_turn(
            {"model": "ui", "messages": [{"role": "user", "content": "  "}]}
        )
