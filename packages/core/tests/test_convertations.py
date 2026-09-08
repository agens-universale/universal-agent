import unittest

from gc_core.utils.convertations import output_chat_completion_to_response, get_output_list


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

    def test_output_chat_completion_raises_meaningful_error_for_choice_error(self):
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

        self.assertIn("provider returned choice error", str(ctx.exception).lower())
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
    return {"choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}]}


def test_get_output_list_accepts_unknown_finish_with_content():
    # раньше падало ValueError("Unknown finish reason"); теперь принимает
    out = get_output_list(_dct("content_filter", content="текст"))
    assert any(o["type"] == "message" for o in out)

def test_get_output_list_rejects_unknown_finish_when_empty():
    import pytest
    with pytest.raises(ValueError):
        get_output_list(_dct(None, content=""))   # пусто → внятный raise


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
    assert msg["content"][0]["text"] == "<think>t</think>ok"


def test_get_output_list_persist_copies_reasoning_content():
    out = get_output_list(_dct_with_reasoning(), persist_reasoning_content=True)
    msg = next(o for o in out if o["type"] == "message")
    assert msg["reasoning_content"] == "secret"
    assert msg["content"][0]["text"] == "<think>t</think>ok"


def test_get_output_list_persist_omits_missing_reasoning():
    out = get_output_list(_dct("stop", content="ok"), persist_reasoning_content=True)
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


if __name__ == "__main__":
    unittest.main()
