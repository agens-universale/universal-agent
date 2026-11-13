import unittest

from gc_core.utils.connect_backend import (
    acreate_call_headers,
    build_call_headers,
)
from gc_testkit.settings import fake_ctx


class _StateStub:
    state_id = "state-1"
    x_target = None


class ChatCompletionsHeadersTest(unittest.IsolatedAsyncioTestCase):
    def test_uses_generic_chat_completion_headers(self):
        ctx = fake_ctx(
            {
                "agent.x_cl_id": "12345",
                "backend.api_mode": "chat_completions",
                "backend.chat_completion_headers": {
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
        ctx = fake_ctx(
            {
                "agent.x_cl_id": "12345",
                "backend.api_mode": "chat_completions",
                "backend.chat_completion_headers": {
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

        ctx = fake_ctx(
            {
                "agent.x_cl_id": "12345",
                "backend.api_mode": "chat_completions",
                "backend.chat_completion_headers": {
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
        ctx = fake_ctx(
            {
                "agent.x_cl_id": "12345",
                "backend.api_mode": "chat_completions",
                "backend.chat_completion_headers": {
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


def test_explicit_chat_mode_uses_model_headers_in_responses():
    from gc_core.model_router import ResolvedModel

    ctx = fake_ctx({"backend.api_mode": "responses"}, auth_connector=None)
    resolved = ResolvedModel(
        alias="eyes",
        model="vision-model",
        url="https://example.test/chat",
        authorization="Bearer eyes-token",
        backend_overrides={"chat_completion_headers": {"x-target": "eyes"}},
        tariff=None,
        source="test",
    )
    headers = build_call_headers(
        _StateStub(), ctx, resolved, api_mode="chat_completions"
    )
    assert headers["Authorization"] == "Bearer eyes-token"
    assert headers["x-target"] == "eyes"
    assert "Authorization" not in build_call_headers(
        _StateStub(), ctx, resolved
    )


def test_string_instead_of_headers_or_kwargs_is_a_validation_error():
    """Строка на месте словаря раньше молча становилась {} — запрос уходил
    без заголовков. Теперь ошибка валидации без значения (там может быть
    токен); пустая строка (``${HEADERS:-}``) — «не задано»."""
    import pytest
    from gc_core.config import apply_cli_overrides
    from gc_testkit.settings import make_settings
    from pydantic import ValidationError

    for path in (
        "backend.chat_completion_headers",
        "backend.chat_completion_kwargs",
    ):
        with pytest.raises(ValidationError) as exc:
            apply_cli_overrides(
                make_settings(),
                extra_overrides={path: "Authorization: Bearer sk-secret"},
            )
        assert path in str(exc.value)
        assert "sk-secret" not in str(exc.value)
        loaded = apply_cli_overrides(
            make_settings(), extra_overrides={path: ""}
        )
        assert loaded.settings.backend.model_dump()[path.split(".")[1]] == {}


def _probe_settings(**dotted):
    from gc_testkit.settings import make_settings

    base = {
        "agent.x_cl_id": "cl-7",
        "backend.api_mode": "chat_completions",
        "backend.chat_completion_headers": {
            "Authorization": "Bearer shared",
            "X-Gateway-Key": "gw",
        },
    }
    return make_settings({**base, **dotted})


def test_probe_headers_carry_the_agent_extras_without_auth_or_session():
    """Проба идёт с теми же дополнительными заголовками, что агент: шлюз,
    требующий свой заголовок, иначе отвечает пробе 401 при рабочем агенте.
    Authorization подбирает сама проба, сессии у неё нет."""
    from gc_core.utils.connect_backend import connection_probe_headers

    headers = connection_probe_headers(_probe_settings())
    assert headers == {"x_cl_id": "cl-7", "X-Gateway-Key": "gw"}


def test_probe_headers_prefer_the_model_overlay_like_the_agent():
    from gc_core.model_router import ResolvedModel
    from gc_core.utils.connect_backend import connection_probe_headers

    resolved = ResolvedModel(
        alias="main",
        model="m",
        url="https://own.test/v1/chat/completions",
        authorization="Bearer own",
        backend_overrides={
            "chat_completion_headers": {
                "authorization": "Bearer per-model",
                "X-Own": "1",
            }
        },
        tariff=None,
        source="test",
    )
    headers = connection_probe_headers(_probe_settings(), resolved)
    assert headers == {"x_cl_id": "cl-7", "X-Own": "1"}


def test_probe_headers_skip_chat_extras_in_responses_mode():
    from gc_core.utils.connect_backend import connection_probe_headers

    settings = _probe_settings(**{"backend.api_mode": "responses"})
    assert connection_probe_headers(settings) == {"x_cl_id": "cl-7"}
