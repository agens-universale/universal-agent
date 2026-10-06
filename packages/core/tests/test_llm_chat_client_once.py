import json
from types import SimpleNamespace

import httpx
import pytest

from gc_core.model_router import ResolvedModel
from gc_core.usage.account import UsageAccount
from gc_core.utils import llm_chat_client as client
from gc_core.utils.backend_errors import EmbeddedChoiceError
from gc_testkit.settings import fake_ctx, update_ctx
from transport_fakes import install_transport, json_response


def _kwargs(route):
    return {
        "stream": True,
        "response_format": {"type": "json_object"},
        "provider": {"order": [route]},
    }


def context():
    return fake_ctx(
        {
            "backend.api_mode": "responses",
            "backend.request_timeout_sec": 3,
            "backend.use_retry": True,
            # Служебный one-shot берёт параметры auxiliary (не main).
            "chat_completions.auxiliary_llm.completion_kwargs": _kwargs(
                "route-a"
            ),
        },
        auth_connector=None,
        logger=None,
    )


def model(**kwargs):
    return ResolvedModel(
        alias="eyes",
        model="image-model",
        url="https://example.test/chat",
        authorization="Bearer secret",
        backend_overrides=kwargs,
        tariff=None,
        source="test",
    )


def state():
    return SimpleNamespace(
        state_id="thread", x_target=None, usage=UsageAccount()
    )


@pytest.mark.parametrize("failure", ["500", "401", "timeout", "embedded"])
async def test_once_client_has_one_actual_attempt(monkeypatch, failure):
    response = {
        "500": httpx.Response(500),
        "401": httpx.Response(401),
        "timeout": httpx.ReadTimeout("timeout"),
        "embedded": json_response(
            {"error": {"code": 503, "message": "unavailable"}}
        ),
    }[failure]

    def handler(request):
        if isinstance(response, Exception):
            raise response
        return response(request) if callable(response) else response

    spy = install_transport(monkeypatch, handler)
    ctx, st = context(), state()
    prepared = await client.aprepare_chat_completion(ctx, st, resolved=model())
    with pytest.raises(
        (httpx.HTTPStatusError, httpx.ReadTimeout, EmbeddedChoiceError)
    ):
        await client.arequest_chat_completion_once(
            ctx,
            st,
            prepared=prepared,
            messages=[{"role": "user", "content": "read"}],
            max_tokens=512,
            request_label="vision_probe",
        )
    assert len(spy.requests) == 1


async def test_snapshot_budget_finish_reason_and_usage(monkeypatch):
    spy = install_transport(
        monkeypatch,
        json_response(
            {
                "choices": [
                    {
                        "message": {"content": " ABC234 "},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 6},
            }
        ),
    )
    ctx, st = context(), state()
    prepared = await client.aprepare_chat_completion(ctx, st, resolved=model())
    # Снимок уже сделан: правка настроек после него запрос не меняет.
    update_ctx(
        ctx,
        **{
            "chat_completions.auxiliary_llm.completion_kwargs": _kwargs(
                "route-b"
            )
        },
    )
    result = await client.arequest_chat_completion_once(
        ctx,
        st,
        prepared=prepared,
        messages=[{"role": "user", "content": "read"}],
        max_tokens=512,
        request_label="vision_probe",
    )
    assert result == client.ChatCompletionText("ABC234", "length")
    body = json.loads(spy.requests[0].content)
    assert body["max_completion_tokens"] == 512
    assert body["provider"] == {"order": ["route-a"]}
    assert not body.get("stream")
    assert "tools" not in body and "response_format" not in body
    assert st.usage.by_category["vision"]["calls"] == 1
    assert st.usage.by_category["vision"]["input"] == 12
    assert "secret" not in repr(prepared)
