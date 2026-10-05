import base64
import io
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from gc_core.utils.llm_chat_client import (
    ChatCompletionText,
    aprepare_chat_completion,
)
from gc_core.vision.probe import (
    ProbeStatus,
    aprobe_route,
    classify_answer,
    make_challenge,
)
from PIL import Image
from test_llm_chat_client_once import context, model, state


@pytest.mark.parametrize(
    "text,finish,status",
    [
        (" K7M4RX\n", "stop", ProbeStatus.CONFIRMED),
        ("The code is K7M4RX", "stop", ProbeStatus.UNCONFIRMED),
        ("", "stop", ProbeStatus.UNCONFIRMED),
        ("", "length", ProbeStatus.INSUFFICIENT_BUDGET),
        ("K7M", "max_tokens", ProbeStatus.INSUFFICIENT_BUDGET),
        ("K7M4RX", "length", ProbeStatus.CONFIRMED),
    ],
)
def test_answer_classification(text, finish, status):
    assert (
        classify_answer("K7M4RX", ChatCompletionText(text, finish)).status
        is status
    )


def test_challenges_are_private_distinct_and_legible():
    first = make_challenge(retry=False)
    second = make_challenge(retry=True, previous_code=first.code)
    assert len(first.code) == 6 and set(first.code) <= set(
        "2345679ACDEFGHJKMNPRUVWXY"
    )
    assert first.code != second.code
    for challenge, size in [(first, (320, 120)), (second, (384, 192))]:
        image = Image.open(io.BytesIO(challenge.png))
        assert image.size == size and image.format == "PNG"
        assert not image.info
        assert challenge.code.encode() not in challenge.png
        assert image.getextrema() == (0, 255)
        assert challenge.code not in repr(challenge)


async def test_success_is_isolated_one_request(monkeypatch, probe_http):
    from gc_core.vision import probe

    challenge = make_challenge(retry=False)
    monkeypatch.setattr(probe, "make_challenge", lambda **kwargs: challenge)
    requests, responses = probe_http
    responses.append(
        httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": challenge.code},
                        "finish_reason": "stop",
                    }
                ]
            },
        )
    )
    ctx, st = context(), state()
    result = await aprobe_route(
        ctx, st, await aprepare_chat_completion(ctx, st, resolved=model())
    )
    assert result.status is ProbeStatus.CONFIRMED
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["max_completion_tokens"] == 512
    assert len(body["messages"]) == 1 and body["messages"][0]["role"] == "user"
    parts = body["messages"][0]["content"]
    assert (
        base64.b64decode(parts[1]["image_url"]["url"].split(",")[1])
        == challenge.png
    )
    assert challenge.code not in parts[0]["text"]
    assert not (
        {"tools", "response_format", "previous_response_id", "stream"}
        & body.keys()
    )


async def test_wrong_read_retries_with_new_larger_image(probe_http):
    requests, responses = probe_http
    for _ in range(2):
        responses.append(
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "wrong"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        )
    ctx, st = context(), state()
    assert (
        await aprobe_route(
            ctx, st, await aprepare_chat_completion(ctx, st, resolved=model())
        )
    ).status is ProbeStatus.UNCONFIRMED
    assert len(requests) == 2
    images = []
    for request in requests:
        body = json.loads(request.content)
        assert body["max_completion_tokens"] == 512
        images.append(
            base64.b64decode(
                body["messages"][0]["content"][1]["image_url"]["url"].split(
                    ","
                )[1]
            )
        )
    assert images[0] != images[1]
    assert [Image.open(io.BytesIO(png)).size for png in images] == [
        (320, 120),
        (384, 192),
    ]


@pytest.mark.parametrize(
    "response,status",
    [
        (
            httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": ""}, "finish_reason": "length"}
                    ]
                },
            ),
            ProbeStatus.INSUFFICIENT_BUDGET,
        ),
        (
            httpx.Response(400, text="model does not support images"),
            ProbeStatus.UNSUPPORTED,
        ),
        (httpx.Response(401), ProbeStatus.BLOCKED),
        (httpx.Response(413), ProbeStatus.BLOCKED),
        (
            httpx.Response(429, headers={"Retry-After": "60"}),
            ProbeStatus.TRANSIENT,
        ),
        (httpx.Response(503), ProbeStatus.TRANSIENT),
        (httpx.ReadTimeout("timeout"), ProbeStatus.TRANSIENT),
        (
            httpx.Response(
                200, json={"error": {"code": 401, "message": "bad key"}}
            ),
            ProbeStatus.BLOCKED,
        ),
    ],
)
async def test_failures_stop_after_one_actual_request(
    probe_http, response, status
):
    requests, responses = probe_http
    responses.append(response)
    ctx, st = context(), state()
    result = await aprobe_route(
        ctx, st, await aprepare_chat_completion(ctx, st, resolved=model())
    )
    assert result.status is status
    assert len(requests) == 1
    if isinstance(response, httpx.Response) and response.status_code == 429:
        assert result.retry_after_sec == 60


async def test_retry_after_http_date(probe_http):
    _, responses = probe_http
    responses.append(
        httpx.Response(
            429,
            headers={
                "Retry-After": format_datetime(
                    datetime.now(UTC) + timedelta(seconds=120),
                    usegmt=True,
                )
            },
        )
    )
    ctx, st = context(), state()
    result = await aprobe_route(
        ctx, st, await aprepare_chat_completion(ctx, st, resolved=model())
    )
    assert 110 < result.retry_after_sec <= 120


@pytest.mark.parametrize(
    "answer,expected_attempts,status",
    [
        ("correct", 1, "confirmed"),
        ("wrong", 2, "unconfirmed"),
        ("503", 1, "transient"),
    ],
)
async def test_probe_logs_safe_result_attempts_and_duration(
    monkeypatch, probe_http, caplog, answer, expected_attempts, status
):
    import logging

    from gc_core.vision import probe

    challenge = make_challenge(retry=False)
    monkeypatch.setattr(probe, "make_challenge", lambda **kwargs: challenge)
    requests, responses = probe_http
    for _ in range(expected_attempts):
        responses.append(
            httpx.Response(503)
            if answer == "503"
            else httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": challenge.code
                                if answer == "correct"
                                else answer
                            },
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        )
    ctx, st = context(), state()
    with caplog.at_level(logging.INFO):
        await aprobe_route(
            ctx, st, await aprepare_chat_completion(ctx, st, resolved=model())
        )
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "vision_probe"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.status == status and record.attempts == expected_attempts
    assert record.duration_ms >= 0
    assert record.finish_reason == (None if answer == "503" else "stop")
    assert "vision_probe" in record.getMessage()
    assert (
        "secret" not in record.getMessage()
        and challenge.code not in record.getMessage()
    )
    assert len(requests) == expected_attempts
