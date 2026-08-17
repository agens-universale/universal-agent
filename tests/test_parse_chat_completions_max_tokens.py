import unittest

from gc_core.config import parse_chat_completions_max_tokens


class ParseChatCompletionsMaxTokensTest(unittest.TestCase):
    def test_int_and_float(self):
        self.assertEqual(parse_chat_completions_max_tokens(16000), 16000)
        self.assertEqual(parse_chat_completions_max_tokens(8096.0), 8096)

    def test_string_k_suffix(self):
        self.assertEqual(parse_chat_completions_max_tokens("16k"), 16000)
        self.assertEqual(parse_chat_completions_max_tokens("32K"), 32000)
        self.assertEqual(parse_chat_completions_max_tokens(" 16 k "), 16000)

    def test_string_plain_number(self):
        self.assertEqual(parse_chat_completions_max_tokens("8192"), 8192)
        self.assertEqual(parse_chat_completions_max_tokens("32_768"), 32768)

    def test_invalid_falls_back(self):
        self.assertEqual(
            parse_chat_completions_max_tokens("not-a-number", default=8096), 8096
        )
        self.assertEqual(parse_chat_completions_max_tokens(True, default=8096), 8096)
        self.assertEqual(parse_chat_completions_max_tokens(0, default=8096), 8096)
        self.assertEqual(parse_chat_completions_max_tokens(-3, default=8096), 8096)


if __name__ == "__main__":
    unittest.main()
