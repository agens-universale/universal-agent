import httpx
import pytest

from gc_core.utils.connect_backend import (
    arequest_backend_chat_completions,
    arequest_backend_responses,
)
from transport_fakes import install_transport, sse_body


@pytest.mark.parametrize("mode", ["chat", "stream", "responses"])
async def test_backoff_keeps_the_alternative_after_authentication_rejection(
    monkeypatch, mode
):
    from gc_core.utils import aretry

    async def no_sleep(_delay):
        pass

    monkeypatch.setattr(aretry.asyncio, "sleep", no_sleep)
    accepted_calls = 0

    def handler(request):
        nonlocal accepted_calls
        if request.headers["Authorization"] == "secret":
            return httpx.Response(401)
        accepted_calls += 1
        if accepted_calls == 1:
            return httpx.Response(500)
        if mode == "responses":
            return httpx.Response(
                200,
                text=sse_body(
                    {
                        "type": "response.completed",
                        "response": {"output": "ok"},
                    },
                    done=False,
                ),
            )
        if mode == "stream":
            return httpx.Response(
                200,
                text=sse_body(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "ok"},
                                "finish_reason": "stop",
                            }
                        ]
                    }
                ),
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    spy = install_transport(monkeypatch, handler)
    headers = {"Authorization": "secret"}
    if mode == "responses":
        await arequest_backend_responses(
            {}, headers, "https://example.test/chat", use_retry=True
        )
    else:
        await arequest_backend_chat_completions(
            {},
            headers,
            "https://example.test/chat",
            use_retry=True,
            on_delta=(lambda *args: None) if mode == "stream" else None,
        )
    assert [r.headers["Authorization"] for r in spy.requests] == [
        "secret",
        "Bearer secret",
        "Bearer secret",
    ]
    assert headers == {"Authorization": "secret"}


@pytest.mark.parametrize(
    "initial,accepted",
    [("Bearer secret", "secret"), ("secret", "Bearer secret")],
)
@pytest.mark.parametrize("mode", ["chat", "stream", "responses"])
async def test_authentication_alternative_keeps_payload_and_original_headers(
    monkeypatch, initial, accepted, mode
):
    def handler(request):
        if request.headers["Authorization"] != accepted:
            return httpx.Response(401)
        if mode == "responses":
            return httpx.Response(
                200,
                text=sse_body(
                    {
                        "type": "response.completed",
                        "response": {"output": "ok"},
                    },
                    done=False,
                ),
            )
        if mode == "stream":
            return httpx.Response(
                200,
                text=sse_body(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "ok"},
                                "finish_reason": "stop",
                            }
                        ]
                    }
                ),
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    spy = install_transport(monkeypatch, handler)
    headers = {"Authorization": initial, "X-Test": "kept"}
    payload = {"model": "test", "messages": []}
    if mode == "responses":
        result, _ = await arequest_backend_responses(
            payload, headers, "https://example.test/chat"
        )
        assert result["output"] == "ok"
    else:
        result = await arequest_backend_chat_completions(
            payload,
            headers,
            "https://example.test/chat",
            use_retry=False,
            on_delta=(lambda *args: None) if mode == "stream" else None,
        )
        assert result["choices"][0]["message"]["content"] == "ok"
    assert [r.headers["Authorization"] for r in spy.requests] == [
        initial,
        accepted,
    ]
    assert spy.requests[0].content == spy.requests[1].content
    assert headers == {"Authorization": initial, "X-Test": "kept"}


@pytest.mark.parametrize(
    "status,token,count",
    [
        (401, "secret", 2),
        (403, "secret", 1),
        (500, "secret", 1),
        (401, "Basic abc", 1),
    ],
)
async def test_authentication_alternative_is_bounded_and_only_on_401(
    monkeypatch, status, token, count
):
    spy = install_transport(
        monkeypatch, lambda request: httpx.Response(status)
    )
    with pytest.raises(httpx.HTTPStatusError):
        await arequest_backend_chat_completions(
            {"model": "test"},
            {"Authorization": token},
            "https://example.test/chat",
            use_retry=False,
        )
    assert len(spy.requests) == count


@pytest.mark.parametrize("mode", ["chat", "stream", "responses"])
@pytest.mark.parametrize("refreshed_accepted", [True, False])
async def test_connector_refresh_happens_once_after_both_old_forms(
    monkeypatch, mode, refreshed_accepted
):
    from types import SimpleNamespace
    from gc_core import processing_responses as pr

    class Auth:
        token = "old"
        force_calls = 0

        def get_token(self, force=False):
            if force:
                self.force_calls += 1
                self.token = "new"
            return self.token

    def handler(request):
        if (
            not refreshed_accepted
            or request.headers["Authorization"] != "Bearer new"
        ):
            return httpx.Response(401)
        if mode == "responses":
            return httpx.Response(
                200,
                text=sse_body(
                    {"type": "response.completed", "response": {"id": "ok"}},
                    done=False,
                ),
            )
        if mode == "stream":
            return httpx.Response(
                200,
                text=sse_body(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "ok"},
                                "finish_reason": "stop",
                            }
                        ]
                    }
                ),
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    auth = Auth()
    spy = install_transport(monkeypatch, handler)

    async def once(messages, state, ctx, **kwargs):
        headers = {"Authorization": "Bearer " + auth.token}
        if mode == "responses":
            await arequest_backend_responses(
                {}, headers, "https://example.test/chat"
            )
        else:
            await arequest_backend_chat_completions(
                {},
                headers,
                "https://example.test/chat",
                use_retry=False,
                on_delta=(lambda *args: None) if mode == "stream" else None,
            )
        return [], "ok", {}

    monkeypatch.setattr(pr, "_arequest_backend_canonical_once", once)
    ctx = SimpleNamespace(config_dict={}, auth_connector=auth, logger=None)
    if refreshed_accepted:
        assert await pr._arequest_backend_canonical([], object(), ctx) == (
            [],
            "ok",
            {},
        )
    else:
        with pytest.raises(httpx.HTTPStatusError):
            await pr._arequest_backend_canonical([], object(), ctx)
    assert auth.force_calls == 1
    assert [r.headers["Authorization"] for r in spy.requests] == [
        "Bearer old",
        "old",
        "Bearer new",
    ] + ([] if refreshed_accepted else ["new"])


async def test_timeout_does_not_change_auth_form(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("timeout")

    spy = install_transport(monkeypatch, handler)
    with pytest.raises(httpx.ReadTimeout):
        await arequest_backend_chat_completions(
            {},
            {"Authorization": "secret"},
            "https://example.test/chat",
            use_retry=False,
        )
    assert len(spy.requests) == 1


async def test_no_auth_stays_without_header(monkeypatch):
    spy = install_transport(monkeypatch)
    await arequest_backend_chat_completions(
        {}, {}, "https://example.test/chat", use_retry=False
    )
    assert len(spy.requests) == 1
    assert "Authorization" not in spy.requests[0].headers


def _bearer_only(request):
    if not request.headers["Authorization"].startswith("Bearer "):
        return httpx.Response(401)
    return httpx.Response(
        200, json={"choices": [{"message": {"content": "ok"}}]}
    )


async def _chat(url, *, token="secret", model="m", **kwargs):
    return await arequest_backend_chat_completions(
        {"model": model},
        {"Authorization": token},
        url,
        use_retry=False,
        **kwargs,
    )


@pytest.mark.parametrize("mode", ["chat", "stream", "responses"])
async def test_accepted_form_is_remembered_for_next_requests(
    monkeypatch, mode
):
    def handler(request):
        if request.headers["Authorization"] != "Bearer secret":
            return httpx.Response(401)
        if mode == "responses":
            return httpx.Response(
                200,
                text=sse_body(
                    {
                        "type": "response.completed",
                        "response": {"output": "ok"},
                    },
                    done=False,
                ),
            )
        if mode == "stream":
            return httpx.Response(
                200,
                text=sse_body(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "ok"},
                                "finish_reason": "stop",
                            }
                        ]
                    }
                ),
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    spy = install_transport(monkeypatch, handler)
    for _ in range(2):
        if mode == "responses":
            await arequest_backend_responses(
                {}, {"Authorization": "secret"}, "https://example.test/chat"
            )
        else:
            await _chat(
                "https://example.test/chat",
                on_delta=(lambda *args: None) if mode == "stream" else None,
            )
    assert [r.headers["Authorization"] for r in spy.requests] == [
        "secret",
        "Bearer secret",
        "Bearer secret",
    ]


async def test_each_url_learns_its_own_form(monkeypatch):
    spy = install_transport(monkeypatch, _bearer_only)
    await _chat("https://example.test/v1/chat/completions")
    await _chat("https://example.test/v1/other")
    assert [r.headers["Authorization"] for r in spy.requests] == [
        "secret",
        "Bearer secret",
    ] * 2


async def test_values_written_differently_do_not_clobber_each_other(
    monkeypatch,
):
    spy = install_transport(monkeypatch, _bearer_only)
    await _chat("https://example.test/chat", token="a")
    await _chat("https://example.test/chat", token="Bearer b")
    await _chat("https://example.test/chat", token="a")
    assert [r.headers["Authorization"] for r in spy.requests] == [
        "a",
        "Bearer a",
        "Bearer b",
        "Bearer a",
    ]


async def test_alternative_form_written_in_request_keeps_learned_form(
    monkeypatch,
):
    spy = install_transport(monkeypatch, _bearer_only)
    await _chat("https://example.test/chat", token="x")
    await _chat("https://example.test/chat", token="Bearer x")
    await _chat("https://example.test/chat", token="x")
    assert [r.headers["Authorization"] for r in spy.requests] == [
        "x",
        "Bearer x",
        "Bearer x",
        "Bearer x",
    ]


async def test_models_on_one_url_remember_their_own_forms(monkeypatch):
    import json

    def handler(request):
        model = json.loads(request.content)["model"]
        accepted = "k" if model == "light" else "Bearer k"
        if request.headers["Authorization"] != accepted:
            return httpx.Response(401)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    spy = install_transport(monkeypatch, handler)
    for _ in range(2):
        await _chat("https://gw.test/chat", token="k", model="light")
        await _chat("https://gw.test/chat", token="k", model="default")
    assert [r.headers["Authorization"] for r in spy.requests] == [
        "k",
        "k",
        "Bearer k",
        "k",
        "Bearer k",
    ]


async def test_memory_keeps_no_token(monkeypatch):
    from gc_core.utils import http_auth

    install_transport(monkeypatch, _bearer_only)
    await _chat("https://example.test/chat", token="sk-memory-guard")
    assert http_auth._ACCEPTED_FORMS
    assert "sk-memory-guard" not in repr(http_auth._ACCEPTED_FORMS)


async def test_new_key_written_the_same_way_reuses_learned_form(
    monkeypatch,
):
    spy = install_transport(monkeypatch, _bearer_only)
    await _chat("https://example.test/chat", token="t1")
    await _chat("https://example.test/chat", token="t2")
    assert [r.headers["Authorization"] for r in spy.requests] == [
        "t1",
        "Bearer t1",
        "Bearer t2",
    ]


async def test_other_server_does_not_inherit_remembered_form(monkeypatch):
    spy = install_transport(monkeypatch, _bearer_only)
    await _chat("https://a.example.test/chat")
    await _chat("https://b.example.test/chat")
    await _chat("http://a.example.test/chat")
    await _chat("https://a.example.test:8443/chat")
    assert [r.headers["Authorization"] for r in spy.requests] == [
        "secret",
        "Bearer secret",
    ] * 4


async def test_form_is_remembered_only_after_success(monkeypatch):
    statuses = iter([401, 500, 401, 200])
    spy = install_transport(
        monkeypatch,
        lambda request: httpx.Response(
            next(statuses), json={"choices": [{"message": {}}]}
        ),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await _chat("https://example.test/chat")
    await _chat("https://example.test/chat")
    assert [r.headers["Authorization"] for r in spy.requests] == [
        "secret",
        "Bearer secret",
    ] * 2


async def test_rejected_alternative_does_not_lock_out_configured_form(
    monkeypatch,
):
    statuses = iter([401, 403, 200])
    spy = install_transport(
        monkeypatch,
        lambda request: httpx.Response(
            next(statuses), json={"choices": [{"message": {}}]}
        ),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await _chat("https://example.test/chat")
    await _chat("https://example.test/chat")
    assert [r.headers["Authorization"] for r in spy.requests] == [
        "secret",
        "Bearer secret",
        "secret",
    ]


async def test_remembered_form_switches_back_after_its_rejection(
    monkeypatch,
):
    accepted = "Bearer secret"

    def handler(request):
        if request.headers["Authorization"] != accepted:
            return httpx.Response(401)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    spy = install_transport(monkeypatch, handler)
    await _chat("https://example.test/chat")
    accepted = "secret"
    await _chat("https://example.test/chat")
    await _chat("https://example.test/chat")
    assert [r.headers["Authorization"] for r in spy.requests] == [
        "secret",
        "Bearer secret",
        "Bearer secret",
        "secret",
        "secret",
    ]


async def test_single_attempt_client_keeps_configured_form(monkeypatch):
    spy = install_transport(monkeypatch, _bearer_only)
    await _chat("https://example.test/chat")
    with pytest.raises(httpx.HTTPStatusError):
        await _chat("https://example.test/chat", allow_auth_retry=False)
    assert [r.headers["Authorization"] for r in spy.requests] == [
        "secret",
        "Bearer secret",
        "secret",
    ]


async def test_invalid_port_fails_in_transport_not_in_memory(monkeypatch):
    install_transport(monkeypatch, _bearer_only)
    with pytest.raises(httpx.InvalidURL):
        await _chat("http://localhost:80a/chat")


async def test_auxiliary_request_starts_with_form_learned_by_main_request(
    monkeypatch,
):
    from types import SimpleNamespace

    from gc_core.utils import llm_chat_client
    from gc_testkit import as_coro
    from gc_testkit.settings import fake_ctx

    url = "https://example.test/v1/chat/completions"
    spy = install_transport(monkeypatch, _bearer_only)
    monkeypatch.setattr(
        llm_chat_client,
        "acreate_call_headers",
        as_coro(lambda _s, _c, resolved=None: {"Authorization": "secret"}),
    )
    await _chat(url)
    ctx = fake_ctx({"backend.completions_url": url}, logger=None)
    await llm_chat_client.arequest_chat_completion_text(
        ctx=ctx,
        state=SimpleNamespace(default_model="m"),
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=10,
        request_label="summary_batch_1",
        consumer="summary",
    )
    assert [r.headers["Authorization"] for r in spy.requests] == [
        "secret",
        "Bearer secret",
        "Bearer secret",
    ]
