import unittest


from utils.connect_backend import normalize_chat_completions_tools


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


if __name__ == "__main__":
    unittest.main()

