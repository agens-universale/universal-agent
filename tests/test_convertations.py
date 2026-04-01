import unittest

from utils.convertations import output_chat_completion_to_response


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


if __name__ == "__main__":
    unittest.main()
