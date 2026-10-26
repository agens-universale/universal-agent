import unittest
from types import SimpleNamespace

from gc_core.utils.connect_backend import (
    create_call_data_completions,
    normalize_chat_completions_tools,
)
from gc_testkit.settings import make_settings


class ChatCompletionsToolsNormalizationTest(unittest.TestCase):
    def test_normalizes_flat_function_tool_spec(self):
        tools = [
            {
                "type": "function",
                "name": "fs_write_file",
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
        self.assertEqual(normalized[0]["function"]["name"], "fs_write_file")
        self.assertIn("parameters", normalized[0]["function"])
        self.assertEqual(normalized[0]["function"]["strict"], True)

    def test_keeps_already_nested_function_tool_spec(self):
        tools = [
            {
                "type": "function",
                "function": {"name": "x", "parameters": {"type": "object"}},
            }
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

    def test_create_call_data_completions_deduplicates_without_id_by_payload(
        self,
    ):
        state = SimpleNamespace(
            messages=[{"role": "user", "content": "same message"}],
            tools=[],
            default_model="test-model",
        )
        incoming = [{"role": "user", "content": "same message"}]

        data = create_call_data_completions(incoming, state=state)
        self.assertIn("messages", data)
        self.assertEqual(len(data["messages"]), 1)

    def test_create_call_data_completions_keeps_message_mapping_without_business_sanitization(
        self,
    ):
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
                "name": "fs_write_file",
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

    def test_create_call_data_completions_coalesces_parallel_function_calls(
        self,
    ):
        state = SimpleNamespace(
            messages=[
                {"role": "user", "content": "task"},
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "search_web_search",
                    "arguments": "{}",
                },
                {
                    "id": "fc_2",
                    "type": "function_call",
                    "call_id": "call_2",
                    "name": "search_web_search",
                    "arguments": "{}",
                },
                {
                    "id": "fco_1",
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "r1",
                },
                {
                    "id": "fco_2",
                    "type": "function_call_output",
                    "call_id": "call_2",
                    "output": "r2",
                },
            ],
            tools=[],
            default_model="test-model",
        )
        data = create_call_data_completions([], state=state)
        assistants_with_tools = [
            m
            for m in data["messages"]
            if m.get("role") == "assistant" and m.get("tool_calls")
        ]
        self.assertEqual(len(assistants_with_tools), 1)
        self.assertEqual(len(assistants_with_tools[0]["tool_calls"]), 2)

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
                "name": "fs_write_file",
                "arguments": '{"path": "/tmp/demo.md", "content": "x"}',
                "status": "completed",
            }
        ]

        data = create_call_data_completions(incoming, state=state)
        self.assertIn("messages", data)
        self.assertEqual(len(data["messages"]), 1)
        self.assertEqual(data["messages"][0]["role"], "assistant")
        self.assertIn("tool_calls", data["messages"][0])
        self.assertEqual(len(data["messages"][0]["tool_calls"]), 1)

    def test_create_call_data_completions_explicit_max_tokens_uses_max_completion_tokens_by_default(
        self,
    ):
        state = SimpleNamespace(messages=[], tools=[], default_model="m")
        data = create_call_data_completions(
            [{"role": "user", "content": "hi"}], state=state, max_tokens=12000
        )
        self.assertEqual(data["max_completion_tokens"], 12000)
        self.assertNotIn("max_tokens", data)

    def test_create_call_data_completions_explicit_max_tokens_uses_max_tokens_with_flag(
        self,
    ):
        state = SimpleNamespace(messages=[], tools=[], default_model="m")
        data = create_call_data_completions(
            [{"role": "user", "content": "hi"}],
            state=state,
            max_tokens=12000,
            settings=make_settings({"backend.use_max_tokens_param": True}),
        )
        self.assertEqual(data["max_tokens"], 12000)
        self.assertNotIn("max_completion_tokens", data)

    def test_create_call_data_completions_uses_max_completion_tokens_by_default(
        self,
    ):
        state = SimpleNamespace(messages=[], tools=[], default_model="m")
        data = create_call_data_completions(
            [{"role": "user", "content": "hi"}],
            state=state,
            settings=make_settings(
                {"backend.chat_completions_max_tokens": 7777}
            ),
        )
        self.assertEqual(data["max_completion_tokens"], 7777)
        self.assertNotIn("max_tokens", data)

    def test_create_call_data_completions_honors_use_max_tokens_param_flag(
        self,
    ):
        state = SimpleNamespace(messages=[], tools=[], default_model="m")
        data = create_call_data_completions(
            [{"role": "user", "content": "hi"}],
            state=state,
            settings=make_settings(
                {
                    "backend.chat_completions_max_tokens": 5555,
                    "backend.use_max_tokens_param": True,
                }
            ),
        )
        self.assertEqual(data["max_tokens"], 5555)
        self.assertNotIn("max_completion_tokens", data)

    def test_create_call_data_completions_merges_chat_completion_kwargs_safely(
        self,
    ):
        state = SimpleNamespace(messages=[], tools=[], default_model="m")
        data = create_call_data_completions(
            [{"role": "user", "content": "hi"}],
            state=state,
            settings=make_settings(
                {
                    "backend.chat_completion_kwargs": {
                        "temperature": 0.2,
                        "top_p": 0.9,
                        "model": "must_not_override",
                        "messages": [{"role": "user", "content": "override"}],
                        "max_tokens": 1,
                        "tool_choice": "required",
                    }
                }
            ),
        )
        self.assertEqual(data["temperature"], 0.2)
        self.assertEqual(data["top_p"], 0.9)
        self.assertEqual(data["model"], "m")
        self.assertEqual(len(data["messages"]), 1)
        self.assertEqual(data["tool_choice"], "required")
        self.assertNotEqual(data.get("max_tokens"), 1)

    def test_create_call_data_completions_omits_unconfigured_generation_kwargs(
        self,
    ):
        state = SimpleNamespace(messages=[], tools=[], default_model="m")
        data = create_call_data_completions(
            [{"role": "user", "content": "hi"}],
            state=state,
            settings=make_settings({"backend.chat_completion_kwargs": {}}),
        )

        self.assertNotIn("tool_choice", data)
        self.assertNotIn("chat_template_kwargs", data)


if __name__ == "__main__":
    unittest.main()
