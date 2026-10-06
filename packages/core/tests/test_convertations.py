import unittest
from unittest.mock import MagicMock

from gc_core.utils.convertations import (
    get_output_list,
    output_chat_completion_to_response,
)


def _base_payload():
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "ok",
                    "tool_calls": [],
                },
                "finish_reason": "stop",
            }
        ],
    }


def test_identical_answers_from_different_responses_have_distinct_ids():
    first = _base_payload()
    second = _base_payload()
    second["id"] = "chatcmpl-second"
    a = output_chat_completion_to_response(first)["output"][0]
    b = output_chat_completion_to_response(second)["output"][0]
    assert a["content"] == b["content"]
    assert a["id"] != b["id"]
    assert output_chat_completion_to_response(first)["output"][0]["id"] == a["id"]


class ConvertationsTest(unittest.TestCase):
    def test_output_chat_completion_uses_created_field(self):
        payload = _base_payload()
        payload["created"] = 123456

        res = output_chat_completion_to_response(payload)

        self.assertEqual(res["created_at"], 123456)
        self.assertNotIn("created", res)
        self.assertEqual(res["object"], "response")
        self.assertEqual(res["status"], "completed")

    def test_output_chat_completion_accepts_created_at_field(self):
        payload = _base_payload()
        payload["created_at"] = 999

        res = output_chat_completion_to_response(payload)

        self.assertEqual(res["created_at"], 999)
        self.assertNotIn("created", res)
        self.assertEqual(res["output"][0]["type"], "message")

    def test_output_chat_completion_raises_meaningful_error_for_choice_error(
        self,
    ):
        payload = _base_payload()
        payload["choices"][0]["finish_reason"] = None
        payload["choices"][0]["message"]["content"] = None
        payload["choices"][0]["error"] = {
            "code": 502,
            "message": "Network connection lost.",
            "metadata": {"error_type": "provider_unavailable"},
        }

        with self.assertRaises(ValueError) as ctx:
            output_chat_completion_to_response(payload)

        self.assertIn(
            "provider returned choice error", str(ctx.exception).lower()
        )
        self.assertIn("provider_unavailable", str(ctx.exception))

    def test_output_chat_completion_accepts_finish_reason_length(self):
        payload = _base_payload()
        payload["choices"][0]["finish_reason"] = "length"
        payload["choices"][0]["message"]["tool_calls"] = [
            {
                "id": "call_x",
                "type": "function",
                "function": {"name": "f", "arguments": "{}"},
            }
        ]

        res = output_chat_completion_to_response(payload, logger_override=None)

        types_out = [item["type"] for item in res["output"]]
        self.assertIn("message", types_out)
        self.assertIn("function_call", types_out)


def _dct(finish_reason, content="ответ", tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {
        "choices": [
            {"index": 0, "message": msg, "finish_reason": finish_reason}
        ]
    }


def test_get_output_list_accepts_blocked_finish_with_content():
    # content_filter — известный blocked, не ValueError и не unknown warning
    out = get_output_list(_dct("content_filter", content="текст"))
    assert any(o["type"] == "message" for o in out)


def test_get_output_list_accepts_unknown_finish_with_content():
    # раньше падало ValueError("Unknown finish reason"); теперь принимает
    out = get_output_list(_dct("eos", content="текст"))
    assert any(o["type"] == "message" for o in out)


def test_get_output_list_empty_unknown_finish_is_empty_batch():
    # Случай C: пустой choice — не ValueError транспорта, цикл bounce сам.
    assert get_output_list(_dct(None, content="")) == []
    assert get_output_list(_dct("stop", content="")) == []
    assert get_output_list(_dct("stop", content=None)) == []


def test_get_output_list_null_message_with_known_finish_is_empty_batch():
    # message: None / нет ключа — не KeyError/AttributeError на dct_choice['message'].
    assert (
        get_output_list(
            {"choices": [{"finish_reason": "stop", "message": None}]}
        )
        == []
    )
    assert get_output_list({"choices": [{"finish_reason": "stop"}]}) == []


def test_get_output_list_whitespace_content_alone_is_empty_batch():
    # Пробельный content схлопывается: пустой батч → bounce, без мусора в истории.
    assert get_output_list(_dct("stop", content="\n")) == []
    assert get_output_list(_dct("stop", content="\n\n")) == []
    assert get_output_list(_dct(None, content="\n")) == []


def test_get_output_list_whitespace_content_with_reasoning_salvages():
    # Qwen3/vLLM при length: content="\n\n" + reasoning → salvage, не два \\n в истории.
    payload = _dct("length", content="\n\n")
    payload["choices"][0]["message"]["reasoning_content"] = (
        "search the leaderboard"
    )
    out = get_output_list(payload)
    msg, text = _message_text(out)
    assert text == "<think>search the leaderboard</think>"
    assert "reasoning_content" not in msg


def test_get_output_list_whitespace_reasoning_alone_is_empty():
    payload = _dct("stop", content=None)
    payload["choices"][0]["message"]["reasoning_content"] = "  \n"
    assert get_output_list(payload) == []


def test_get_output_list_empty_unknown_finish_logs_error():
    log = MagicMock()
    assert get_output_list(_dct(None, content=""), logger_override=log) == []
    log.error.assert_called_once()
    msg = log.error.call_args[0][0] % log.error.call_args[0][1:]
    assert "Empty choice discarded" in msg
    assert "finish_reason=None" in msg
    log.warning.assert_not_called()


def test_get_output_list_blocked_finish_with_content_no_unknown_warning():
    log = MagicMock()
    out = get_output_list(
        _dct("content_filter", content="текст"), logger_override=log
    )
    assert any(o["type"] == "message" for o in out)
    log.warning.assert_not_called()
    log.error.assert_not_called()


def test_get_output_list_unknown_finish_with_content_logs_warning():
    log = MagicMock()
    out = get_output_list(_dct("eos", content="текст"), logger_override=log)
    assert any(o["type"] == "message" for o in out)
    log.warning.assert_called_once()
    msg = log.warning.call_args[0][0] % log.warning.call_args[0][1:]
    assert "content/tool_calls/reasoning present" in msg
    assert "eos" in msg
    log.error.assert_not_called()


def test_get_output_list_unknown_finish_reasoning_only_logs_warning_not_error():
    log = MagicMock()
    payload = _dct(None, content=None)
    payload["choices"][0]["message"]["reasoning_content"] = (
        "try another approach"
    )
    out = get_output_list(payload, logger_override=log)
    _, text = _message_text(out)
    assert text == "<think>try another approach</think>"
    log.warning.assert_called_once()
    msg = log.warning.call_args[0][0] % log.warning.call_args[0][1:]
    assert "content/tool_calls/reasoning present" in msg
    log.error.assert_not_called()


def _dct_with_reasoning(content="<think>t</think>ok", reasoning="secret"):
    return {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "reasoning_content": reasoning,
                },
                "finish_reason": "stop",
            }
        ]
    }


def test_get_output_list_default_drops_reasoning_content():
    out = get_output_list(_dct_with_reasoning())
    msg = next(o for o in out if o["type"] == "message")
    assert "reasoning_content" not in msg
    assert msg["content"][0]["text"] == "ok"


def test_get_output_list_persist_copies_reasoning_content():
    out = get_output_list(
        _dct_with_reasoning(), persist_reasoning_content=True
    )
    msg = next(o for o in out if o["type"] == "message")
    assert msg["reasoning_content"] == "secret"
    assert msg["content"][0]["text"] == "<think>t</think>ok"


def test_get_output_list_persist_omits_missing_reasoning():
    out = get_output_list(
        _dct("stop", content="ok"), persist_reasoning_content=True
    )
    msg = next(o for o in out if o["type"] == "message")
    assert "reasoning_content" not in msg


def _tool_call(call_id="call_1", name="read_file", arguments="{}"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def test_get_output_list_keeps_reasoning_when_content_null_with_tool_calls():
    """Non-stream tool-only: content=null + reasoning + tools → message-носитель, не поле на fc."""
    payload = _dct("tool_calls", content=None, tool_calls=[_tool_call()])
    payload["choices"][0]["message"]["reasoning_content"] = "why this tool"
    out = get_output_list(payload, persist_reasoning_content=True)
    assert [o["type"] for o in out] == ["message", "function_call"]
    msg = out[0]
    assert msg["content"][0]["text"] == ""
    assert msg["reasoning_content"] == "why this tool"
    assert "reasoning_content" not in out[1]


def test_get_output_list_null_content_tool_call_skips_message_without_reasoning():
    payload = _dct("tool_calls", content=None, tool_calls=[_tool_call()])
    out = get_output_list(payload, persist_reasoning_content=True)
    assert [o["type"] for o in out] == ["function_call"]


def test_get_output_list_default_drops_reasoning_on_null_content_tool_call():
    payload = _dct("tool_calls", content=None, tool_calls=[_tool_call()])
    payload["choices"][0]["message"]["reasoning_content"] = "why this tool"
    out = get_output_list(payload)
    assert [o["type"] for o in out] == ["function_call"]
    assert all("reasoning_content" not in o for o in out)


def _message_text(out):
    msg = next(o for o in out if o["type"] == "message")
    return msg, msg["content"][0]["text"]


def test_get_output_list_salvages_reasoning_only_into_think_tags():
    payload = _dct("stop", content=None)
    payload["choices"][0]["message"]["reasoning_content"] = (
        "search the leaderboard"
    )
    out = get_output_list(payload)
    msg, text = _message_text(out)
    assert text == "<think>search the leaderboard</think>"
    assert "reasoning_content" not in msg
    assert all(o["type"] != "function_call" for o in out)


def test_get_output_list_salvage_neutralizes_literal_think_close():
    payload = _dct("stop", content=None)
    payload["choices"][0]["message"]["reasoning_content"] = (
        "про </think> теги и ещё </THINK> хвост"
    )
    out = get_output_list(payload)
    msg, text = _message_text(out)
    assert text == "<think>про  теги и ещё  хвост</think>"
    assert text.count("<think>") == 1
    assert text.count("</think>") == 1
    assert "reasoning_content" not in msg


def test_get_output_list_salvages_openrouter_reasoning_alias():
    """Нестрим (бенч) должен спасать reasoning-only так же, как стрим/TUI."""
    payload = _dct("stop", content=None)
    payload["choices"][0]["message"]["reasoning"] = "search the leaderboard"
    out = get_output_list(payload)
    msg, text = _message_text(out)
    assert text == "<think>search the leaderboard</think>"
    assert "reasoning_content" not in msg


def test_get_output_list_reasoning_alias_does_not_double_count():
    payload = _dct("stop", content=None)
    payload["choices"][0]["message"]["reasoning_content"] = "канон"
    payload["choices"][0]["message"]["reasoning"] = "дубль"
    out = get_output_list(payload)
    _, text = _message_text(out)
    assert text == "<think>канон</think>"


def test_get_output_list_salvages_reasoning_only_unknown_finish():
    payload = _dct(None, content=None)
    payload["choices"][0]["message"]["reasoning_content"] = (
        "try another approach"
    )
    out = get_output_list(payload)
    _, text = _message_text(out)
    assert text == "<think>try another approach</think>"


def test_get_output_list_unknown_finish_tools_only_accepted():
    # has_tool_calls в гейт — не через has_payload→has_content.
    payload = _dct(None, content=None, tool_calls=[_tool_call()])
    out = get_output_list(payload)
    assert [o["type"] for o in out] == ["function_call"]


def test_get_output_list_keeps_think_only_content_verbatim():
    out = get_output_list(_dct("stop", content="<think>план</think>"))
    msg, text = _message_text(out)
    assert text == "<think>план</think>"
    assert "reasoning_content" not in msg


def test_get_output_list_think_plus_reasoning_field_does_not_merge():
    payload = _dct("stop", content="<think>план</think>")
    payload["choices"][0]["message"]["reasoning_content"] = "другое"
    out = get_output_list(payload)
    msg, text = _message_text(out)
    assert text == "<think>план</think>"
    assert "reasoning_content" not in msg


def test_get_output_list_tools_mixed_content_keeps_only_prose():
    payload = _dct(
        "tool_calls",
        content="<think>почему тул</think>коротко",
        tool_calls=[_tool_call()],
    )
    out = get_output_list(payload)
    msg, text = _message_text(out)
    assert text == "коротко"
    assert "reasoning_content" not in msg


def test_get_output_list_tools_think_only_content_drops_message():
    """strip_think: think-only + tools → только function_call, мысль не в историю."""
    payload = _dct(
        "tool_calls",
        content="<think>длинная мысль перед тулом</think>",
        tool_calls=[_tool_call()],
    )
    out = get_output_list(payload)
    assert [o["type"] for o in out] == ["function_call"]


def test_get_output_list_persist_tools_think_only_keeps_message():
    payload = _dct(
        "tool_calls",
        content="<think>план</think>",
        tool_calls=[_tool_call()],
    )
    out = get_output_list(payload, persist_reasoning_content=True)
    assert [o["type"] for o in out] == ["message", "function_call"]
    _, text = _message_text(out)
    assert text == "<think>план</think>"


def test_get_output_list_visible_prose_not_salvaged():
    payload = _dct("stop", content="<think>план</think>\nсделаю вывод")
    payload["choices"][0]["message"]["reasoning_content"] = "secret"
    out = get_output_list(payload)
    msg, text = _message_text(out)
    assert text == "сделаю вывод"
    assert "reasoning_content" not in msg


def test_get_output_list_mixed_think_prose_in_content_keeps_only_prose():
    """strip_think: think в канон не кладётся, если есть видимая проза."""
    out = get_output_list(
        _dct("stop", content="<think>огромная мысль</think>короткая проза")
    )
    _, text = _message_text(out)
    assert text == "короткая проза"


def test_get_output_list_literal_unclosed_think_in_prose_kept():
    """Незакрытый ``<think>`` в прозе — литерал, канон не режем."""
    raw = "Use the <think> tag carefully."
    out = get_output_list(_dct("stop", content=raw))
    _, text = _message_text(out)
    assert text == raw


def test_get_output_list_persist_keeps_mixed_think_prose():
    out = get_output_list(
        _dct("stop", content="<think>огромная мысль</think>короткая проза"),
        persist_reasoning_content=True,
    )
    _, text = _message_text(out)
    assert text == "<think>огромная мысль</think>короткая проза"


def test_get_output_list_persist_reasoning_only_does_not_wrap_think():
    payload = _dct("stop", content=None)
    payload["choices"][0]["message"]["reasoning_content"] = "secret"
    out = get_output_list(payload, persist_reasoning_content=True)
    msg, text = _message_text(out)
    assert text == ""
    assert msg["reasoning_content"] == "secret"


def test_get_output_list_persist_copies_openrouter_reasoning_alias():
    payload = _dct("stop", content=None)
    payload["choices"][0]["message"]["reasoning"] = "secret"
    out = get_output_list(payload, persist_reasoning_content=True)
    msg, text = _message_text(out)
    assert text == ""
    assert msg["reasoning_content"] == "secret"


async def test_salvaged_think_is_not_text_answer_even_in_allow():
    from gc_core.intent_guard.text_answer_judge import (
        ashould_return_text_answer,
    )
    from gc_core.message_policy.no_tool_call_bounce import (
        extract_last_assistant_text,
    )
    from gc_core.utils.format_messages import (
        format_message_chat_completions_to_send_backend,
    )

    payload = _dct("stop", content=None)
    payload["choices"][0]["message"]["reasoning_content"] = (
        "search the leaderboard"
    )
    out = get_output_list(payload)
    projected = extract_last_assistant_text(out)
    assert projected == ""
    ctx = type("C", (), {"config_dict": {"text_answer_mode": "allow"}})()
    assert (
        await ashould_return_text_answer(ctx, object(), projected)
    ).accept is False

    outbound = format_message_chat_completions_to_send_backend(out[0])
    assert outbound["content"] == "<think>search the leaderboard</think>"
    assert "reasoning_content" not in outbound


if __name__ == "__main__":
    unittest.main()
