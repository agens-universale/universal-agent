"""`ImageRejects` — знание процесса «эта модель по этому адресу принимает не больше N
картинок за запрос»; N = 0 — не видит изображений (спека изображений §10)."""

import threading

import httpx
import pytest

from gc_core.model_router import ResolvedModel
from vision_fakes import clear_image_rejects
from gc_core.vision.rejects import (
    ImageRejects,
    mark_blind,
    next_image_cap,
    process_image_rejects,
    learn_image_cap,
)

_URL = "http://ecs3:6002/v1/chat/completions"
_FALLBACK = "http://fallback/v1/chat/completions"


def _resolved(url, model) -> ResolvedModel:
    return ResolvedModel(
        alias=None,
        model=model,
        url=url,
        authorization=None,
        backend_overrides={},
        tariff=None,
        source="test",
    )


from gc_core.vision.route import VisionRouteKey
from gc_core.vision.registry import process_vision_probes
from gc_core.vision.probe import ProbeStatus

KEY = VisionRouteKey(_URL, "qwen3.6", "access-one")
OTHER = VisionRouteKey(_URL, "qwen3.6", "access-two")


def test_marks_are_route_specific_and_new_once():
    assert mark_blind(KEY)
    assert not mark_blind(KEY)
    assert process_vision_probes().peek(KEY).status is ProbeStatus.UNSUPPORTED
    assert process_vision_probes().peek(OTHER) is None
    assert process_image_rejects().cap(KEY) is None


def test_concurrent_marks_report_exactly_one_new():
    barrier = threading.Barrier(16)
    results = []

    def mark():
        barrier.wait()
        results.append(mark_blind(KEY))

    threads = [threading.Thread(target=mark) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert sorted(results) == [False] * 15 + [True]


def test_caps_only_decrease_and_separate_access_and_support():
    rejects = ImageRejects()
    assert rejects.cap(KEY) is None
    assert rejects.limit(KEY, 5)
    assert not rejects.limit(KEY, 6)
    assert rejects.limit(KEY, 3)
    assert rejects.cap(KEY) == 3
    assert rejects.cap(OTHER) is None
    trial = rejects.with_limit(KEY, 0)
    assert trial.cap(KEY) == 0 and rejects.cap(KEY) == 3
    assert process_vision_probes().peek(KEY) is None


def test_process_registry_resets_in_place():
    registry = process_image_rejects()
    registry.limit(KEY, 1)
    clear_image_rejects()
    assert registry is process_image_rejects() and registry.cap(KEY) is None


def _http_error(status: int, message: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", _URL)
    response = httpx.Response(
        status, json={"error": {"message": message}}, request=request
    )
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=response
    )


def test_explicit_facts_are_learned_for_the_process():
    """Отказ от зрения и названный предел — факт о модели: запоминается сразу."""
    key = KEY

    assert (
        learn_image_cap(
            key,
            _http_error(
                400, "At most 3 image(s) may be provided in one prompt."
            ),
        )
        is False
    )
    assert process_image_rejects().cap(KEY) == 3
    blind = _http_error(
        400,
        "Model only supports text input; received unsupported content type 'image_url'",
    )
    assert learn_image_cap(key, blind) is True  # стала слепой
    assert learn_image_cap(key, blind) is False


@pytest.mark.parametrize(
    "status, message",
    [
        (413, "Request Entity Too Large"),
        (400, "Too many images in the request."),
    ],
)
def test_inferred_limits_are_not_learned(status, message):
    """413 зависит от объёма истории, «слишком много» без числа — лишь догадка о пределе:
    деление пополам действует только на свой запрос."""
    key = KEY

    assert learn_image_cap(key, _http_error(status, message)) is False
    assert process_image_rejects().cap(KEY) is None


@pytest.mark.parametrize(
    "message, status, sent, cap",
    [
        (
            "At most 0 image(s) may be provided in one prompt.",
            400,
            3,
            0,
        ),  # слепая
        (
            "At most 5 image(s) may be provided in one prompt.",
            400,
            13,
            5,
        ),  # число из текста
        (
            "At most 20 image(s) may be provided in one prompt.",
            400,
            13,
            12,
        ),  # число не меньше посланного
        ("Too many images in the request.", 400, 13, 6),  # без числа — вдвое
        ("Request Entity Too Large", 413, 13, 6),
        (
            "Too many images in the request.",
            400,
            1,
            0,
        ),  # одна не прошла — ни одной
    ],
)
def test_next_cap_is_always_below_what_was_sent(message, status, sent, cap):
    """Каждый повтор шлёт строго меньше картинок: цепочка повторов конечна."""
    assert next_image_cap(_http_error(status, message), sent) == cap
