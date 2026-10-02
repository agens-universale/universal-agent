import unittest
from types import SimpleNamespace

from gc_core.utils.connect_backend import (
    acreate_call_headers,
    build_call_headers,
)


class _StateStub:
    state_id = "state-1"
    x_target = None


class ChatCompletionsHeadersTest(unittest.IsolatedAsyncioTestCase):
    def test_uses_generic_chat_completion_headers(self):
        ctx = SimpleNamespace(
            config_dict={
                "x_cl_id": "12345",
                "backend_api_mode": "chat_completions",
                "chat_completion_headers": {
                    "HTTP-Referer": "https://example.org",
                    "X-OpenRouter-Title": "mini-cli",
                    "X-Custom": "v1",
                },
            },
            auth_connector=None,
        )
        headers = build_call_headers(_StateStub(), ctx)
        self.assertEqual(headers.get("HTTP-Referer"), "https://example.org")
        self.assertEqual(headers.get("X-OpenRouter-Title"), "mini-cli")
        self.assertEqual(headers.get("X-Custom"), "v1")

    def test_uses_chat_completions_token_and_generic_headers(self):
        ctx = SimpleNamespace(
            config_dict={
                "x_cl_id": "12345",
                "backend_api_mode": "chat_completions",
                "chat_completion_headers": {
                    "Authorization": "Bearer sk-or-v1-token",
                    "HTTP-Referer": "https://example.org",
                    "X-OpenRouter-Title": "mini-cli",
                    "X-Title": "mini-cli",
                },
            },
            auth_connector=None,
        )
        headers = build_call_headers(_StateStub(), ctx)
        self.assertEqual(headers.get("Authorization"), "Bearer sk-or-v1-token")
        self.assertEqual(headers.get("HTTP-Referer"), "https://example.org")
        self.assertEqual(headers.get("X-OpenRouter-Title"), "mini-cli")
        self.assertEqual(headers.get("X-Title"), "mini-cli")

    async def test_prefers_chat_completions_token_over_auth_connector(self):
        class _AuthStub:
            def get_token(self):
                return "auth-token"

        ctx = SimpleNamespace(
            config_dict={
                "x_cl_id": "12345",
                "backend_api_mode": "chat_completions",
                "chat_completion_headers": {
                    "Authorization": "Bearer chat-token"
                },
            },
            auth_connector=_AuthStub(),
        )
        # Коннектор задан — значит идём единственной формой операции: токен
        # берётся вне loop, и только потом оверлей заголовков его перебивает.
        headers = await acreate_call_headers(_StateStub(), ctx)
        self.assertEqual(headers.get("Authorization"), "Bearer chat-token")

    def test_chat_completion_headers_override_session_id(self):
        """The overlay must replace the internal state UUID, not duplicate it.

        `GcCliExternal._apply_polar_session` routes Polar trajectories through this key.
        """
        ctx = SimpleNamespace(
            config_dict={
                "x_cl_id": "12345",
                "backend_api_mode": "chat_completions",
                "chat_completion_headers": {
                    "x-session-id": "sympy__sympy-12419"
                },
            },
            auth_connector=None,
        )
        headers = build_call_headers(_StateStub(), ctx)
        self.assertEqual(headers.get("x-session-id"), "sympy__sympy-12419")
        self.assertEqual(
            [k for k in headers if k.lower() == "x-session-id"],
            ["x-session-id"],
        )


if __name__ == "__main__":
    unittest.main()
