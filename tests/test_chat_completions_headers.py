import unittest
from types import SimpleNamespace

from utils.connect_backend import create_call_headers


class _StateStub:
    state_id = "state-1"
    x_target = None


class ChatCompletionsHeadersTest(unittest.TestCase):
    def test_uses_chat_completions_token_and_openrouter_headers(self):
        ctx = SimpleNamespace(
            config_dict={
                "x_cl_id": 12345,
                "backend_api_mode": "chat_completions",
                "chat_completions_auth_token": "sk-or-v1-token",
                "openrouter_http_referer": "https://example.org",
                "openrouter_x_title": "mini-cli",
            },
            auth_connector=None,
        )
        headers = create_call_headers(_StateStub(), ctx)
        self.assertEqual(headers.get("Authorization"), "Bearer sk-or-v1-token")
        self.assertEqual(headers.get("HTTP-Referer"), "https://example.org")
        self.assertEqual(headers.get("X-OpenRouter-Title"), "mini-cli")
        self.assertEqual(headers.get("X-Title"), "mini-cli")

    def test_prefers_chat_completions_token_over_auth_connector(self):
        class _AuthStub:
            def get_token(self):
                return "auth-token"

        ctx = SimpleNamespace(
            config_dict={
                "x_cl_id": 12345,
                "backend_api_mode": "chat_completions",
                "chat_completions_auth_token": "chat-token",
            },
            auth_connector=_AuthStub(),
        )
        headers = create_call_headers(_StateStub(), ctx)
        self.assertEqual(headers.get("Authorization"), "Bearer chat-token")


if __name__ == "__main__":
    unittest.main()
