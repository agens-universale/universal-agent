import unittest

from types import SimpleNamespace

from utils.connect_backend import (
    normalize_chat_completions_tools,
    create_call_data_completions,
)


class ChatCompletionsToolsNormalizationTest(unittest.TestCase):
    def test_normalizes_flat_function_tool_spec(self):
        tools = [
            {
                "type": "function",
                "name": "write_to_file",
                "description": "Writes text content to a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "strict": True,
            }
        ]

        normalized = normalize_chat_completions_tools(tools)
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["type"], "function")
        self.assertIn("function", normalized[0])
        self.assertEqual(normalized[0]["function"]["name"], "write_to_file")
        self.assertIn("parameters", normalized[0]["function"])
        self.assertEqual(normalized[0]["function"]["strict"], True)

    def test_keeps_already_nested_function_tool_spec(self):
        tools = [
            {"type": "function", "function": {"name": "x", "parameters": {"type": "object"}}}
        ]
        normalized = normalize_chat_completions_tools(tools)
        self.assertEqual(normalized, tools)

    def test_create_call_data_completions_handles_messages_without_id(self):
        state = SimpleNamespace(
            messages=[{"role": "user", "content": "old message without id"}],
            tools=[],
            default_model="test-model",
        )
        incoming = [{"role": "user", "content": "new message without id"}]

        data = create_call_data_completions(incoming, state=state)
        self.assertIn("messages", data)
        self.assertEqual(len(data["messages"]), 2)
        self.assertEqual(data["model"], "test-model")

    def test_create_call_data_completions_deduplicates_without_id_by_payload(self):
        state = SimpleNamespace(
            messages=[{"role": "user", "content": "same message"}],
            tools=[],
            default_model="test-model",
        )
        incoming = [{"role": "user", "content": "same message"}]

        data = create_call_data_completions(incoming, state=state)
        self.assertIn("messages", data)
        self.assertEqual(len(data["messages"]), 1)

    def test_create_call_data_completions_keeps_message_mapping_without_business_sanitization(self):
        state = SimpleNamespace(
            messages=[],
            tools=[],
            default_model="test-model",
        )
        incoming = [
            {
                "id": "fc_1",
                "type": "function_call",
                "call_id": "call_1",
                "name": "write_to_file",
                "arguments": '{"path": "/tmp/demo.md"',
                "status": "completed",
            }
        ]

        data = create_call_data_completions(incoming, state=state)
        self.assertIn("messages", data)
        self.assertEqual(len(data["messages"]), 1)
        self.assertEqual(data["messages"][0]["role"], "assistant")
        self.assertIn("tool_calls", data["messages"][0])
        self.assertEqual(
            data["messages"][0]["tool_calls"][0]["function"]["arguments"],
            '{"path": "/tmp/demo.md"',
        )

    def test_create_call_data_completions_keeps_valid_tool_call(self):
        state = SimpleNamespace(
            messages=[],
            tools=[],
            default_model="test-model",
        )
        incoming = [
            {
                "id": "fc_2",
                "type": "function_call",
                "call_id": "call_2",
                "name": "write_to_file",
                "arguments": "{\"path\": \"/tmp/demo.md\", \"content\": \"x\"}",
                "status": "completed",
            }
        ]

        data = create_call_data_completions(incoming, state=state)
        self.assertIn("messages", data)
        self.assertEqual(len(data["messages"]), 1)
        self.assertEqual(data["messages"][0]["role"], "assistant")
        self.assertIn("tool_calls", data["messages"][0])
        self.assertEqual(len(data["messages"][0]["tool_calls"]), 1)


if __name__ == "__main__":
    unittest.main()

