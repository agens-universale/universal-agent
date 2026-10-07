import httpx
import pytest

from gc_core.utils.connect_backend import arequest_backend_chat_completions, arequest_backend_responses
from transport_fakes import install_transport, sse_body


@pytest.mark.parametrize("mode", ["chat", "stream", "responses"])
async def test_backoff_keeps_the_alternative_after_authentication_rejection(monkeypatch, mode):
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
            return httpx.Response(200, text=sse_body({"type": "response.completed", "response": {"output": "ok"}}, done=False))
        if mode == "stream":
            return httpx.Response(200, text=sse_body({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    spy = install_transport(monkeypatch, handler)
    headers = {"Authorization": "secret"}
    if mode == "responses":
        await arequest_backend_responses({}, headers, "https://example.test/chat", use_retry=True)
    else:
        await arequest_backend_chat_completions({}, headers, "https://example.test/chat", use_retry=True,
            on_delta=(lambda *args: None) if mode == "stream" else None)
    assert [r.headers["Authorization"] for r in spy.requests] == ["secret", "Bearer secret", "Bearer secret"]
    assert headers == {"Authorization": "secret"}


@pytest.mark.parametrize("initial,accepted", [("Bearer secret", "secret"), ("secret", "Bearer secret")])
@pytest.mark.parametrize("mode", ["chat", "stream", "responses"])
async def test_authentication_alternative_keeps_payload_and_original_headers(monkeypatch, initial, accepted, mode):
    def handler(request):
        if request.headers["Authorization"] != accepted:
            return httpx.Response(401)
        if mode == "responses":
            return httpx.Response(200, text=sse_body({"type": "response.completed", "response": {"output": "ok"}}, done=False))
        if mode == "stream":
            return httpx.Response(200, text=sse_body({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    spy = install_transport(monkeypatch, handler)
    headers = {"Authorization": initial, "X-Test": "kept"}
    payload = {"model": "test", "messages": []}
    if mode == "responses":
        result, _ = await arequest_backend_responses(payload, headers, "https://example.test/chat")
        assert result["output"] == "ok"
    else:
        result = await arequest_backend_chat_completions(
            payload, headers, "https://example.test/chat", use_retry=False,
            on_delta=(lambda *args: None) if mode == "stream" else None,
        )
        assert result["choices"][0]["message"]["content"] == "ok"
    assert [r.headers["Authorization"] for r in spy.requests] == [initial, accepted]
    assert spy.requests[0].content == spy.requests[1].content
    assert headers == {"Authorization": initial, "X-Test": "kept"}


@pytest.mark.parametrize("status,token,count", [(401, "secret", 2), (403, "secret", 1), (500, "secret", 1), (401, "Basic abc", 1)])
async def test_authentication_alternative_is_bounded_and_only_on_401(monkeypatch, status, token, count):
    spy = install_transport(monkeypatch, lambda request: httpx.Response(status))
    with pytest.raises(httpx.HTTPStatusError):
        await arequest_backend_chat_completions(
            {"model": "test"}, {"Authorization": token}, "https://example.test/chat", use_retry=False
        )
    assert len(spy.requests) == count


@pytest.mark.parametrize("mode", ["chat", "stream", "responses"])
@pytest.mark.parametrize("refreshed_accepted", [True, False])
async def test_connector_refresh_happens_once_after_both_old_forms(monkeypatch, mode, refreshed_accepted):
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
        if not refreshed_accepted or request.headers["Authorization"] != "Bearer new":
            return httpx.Response(401)
        if mode == "responses":
            return httpx.Response(200, text=sse_body({"type": "response.completed", "response": {"id": "ok"}}, done=False))
        if mode == "stream":
            return httpx.Response(200, text=sse_body({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    auth = Auth()
    spy = install_transport(monkeypatch, handler)

    async def once(messages, state, ctx, **kwargs):
        headers = {"Authorization": "Bearer " + auth.token}
        if mode == "responses":
            await arequest_backend_responses({}, headers, "https://example.test/chat")
        else:
            await arequest_backend_chat_completions(
                {}, headers, "https://example.test/chat", use_retry=False,
                on_delta=(lambda *args: None) if mode == "stream" else None,
            )
        return [], "ok", {}

    monkeypatch.setattr(pr, "_arequest_backend_canonical_once", once)
    ctx = SimpleNamespace(config_dict={}, auth_connector=auth, logger=None)
    if refreshed_accepted:
        assert await pr._arequest_backend_canonical([], object(), ctx) == ([], "ok", {})
    else:
        with pytest.raises(httpx.HTTPStatusError):
            await pr._arequest_backend_canonical([], object(), ctx)
    assert auth.force_calls == 1
    assert [r.headers["Authorization"] for r in spy.requests] == ["Bearer old", "old", "Bearer new"] + ([] if refreshed_accepted else ["new"])


async def test_timeout_does_not_change_auth_form(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("timeout")

    spy = install_transport(monkeypatch, handler)
    with pytest.raises(httpx.ReadTimeout):
        await arequest_backend_chat_completions({}, {"Authorization": "secret"}, "https://example.test/chat", use_retry=False)
    assert len(spy.requests) == 1


async def test_no_auth_stays_without_header(monkeypatch):
    spy = install_transport(monkeypatch)
    await arequest_backend_chat_completions({}, {}, "https://example.test/chat", use_retry=False)
    assert len(spy.requests) == 1
    assert "Authorization" not in spy.requests[0].headers
