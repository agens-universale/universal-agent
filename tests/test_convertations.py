import unittest

from utils.convertations import output_chat_completion_to_response, get_output_list


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


if __name__ == "__main__":
    unittest.main()
