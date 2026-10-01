"""`ImageRejects` — знание процесса «эта модель по этому адресу принимает не больше N
картинок за запрос»; N = 0 — не видит изображений (спека изображений §10)."""

import threading

import httpx
import pytest

from gc_core.model_router import ResolvedModel
from gc_core.vision.rejects import (
    ImageRejects,
    mark_blind,
    next_image_cap,
    process_image_rejects,
    learn_image_cap,
    reset_image_rejects_for_tests,
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


def test_mark_is_new_only_once():
    rejects = ImageRejects()

    assert rejects.mark(_URL, "qwen3.6") is True
    assert rejects.mark(_URL, "qwen3.6") is False
    assert rejects.contains(_URL, "qwen3.6") is True


def test_key_is_the_address_and_the_model_together():
    """Одна и та же модель за разными адресами видит по-разному (свой vLLM без
    зрения, OpenRouter со зрением) — пометка одной пары другую не задевает."""
    rejects = ImageRejects()
    rejects.mark(_URL, "qwen3.6")

    assert (
        rejects.contains(
            "https://openrouter.ai/api/v1/chat/completions", "qwen3.6"
        )
        is False
    )
    assert rejects.contains(_URL, "deepseek-v4-flash") is False


def test_concurrent_marks_report_exactly_one_new():
    """Пометку ставят основной путь и тул «глаз» — в том числе из потоков
    субагентов: новой её называет ровно один, иначе уведомление ушло бы дважды."""
    rejects = ImageRejects()
    barrier = threading.Barrier(16)
    results: list = []

    def _mark():
        barrier.wait()
        results.append(rejects.mark(_URL, "qwen3.6"))

    threads = [threading.Thread(target=_mark) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False] * 15 + [True]


def test_process_registry_is_one_object_and_resets_in_place():
    registry = process_image_rejects()
    registry.mark(_URL, "qwen3.6")

    reset_image_rejects_for_tests()

    assert process_image_rejects() is registry
    assert registry.contains(_URL, "qwen3.6") is False


def test_process_registry_leak_probe_marks():
    """Первая половина пробы изоляции: пометка на процесс..."""
    process_image_rejects().mark(_URL, "leak-probe")
    assert process_image_rejects().contains(_URL, "leak-probe") is True


def test_process_registry_starts_empty_in_every_test():
    """...вторая: следующий тест её не видит — сбрасывает autouse-фикстура
    набора (`packages/core/tests/conftest.py`)."""
    assert process_image_rejects().contains(_URL, "leak-probe") is False


def test_mark_blind_keys_by_the_model_address_or_the_fallback():
    """Ключ — фактический адрес запроса: у модели реестра свой, иначе запасной."""
    assert (
        mark_blind(_resolved(_URL, "qwen3.6"), fallback_url=_FALLBACK) is True
    )
    assert (
        mark_blind(_resolved(None, "legacy"), fallback_url=_FALLBACK) is True
    )

    assert process_image_rejects().contains(_URL, "qwen3.6") is True
    assert process_image_rejects().contains(_FALLBACK, "legacy") is True


def test_mark_blind_is_new_only_once():
    resolved = _resolved(_URL, "qwen3.6")
    assert mark_blind(resolved, fallback_url=_FALLBACK) is True
    assert mark_blind(resolved, fallback_url=_FALLBACK) is False


# --- Потолок картинок -------------------------------------------------------


def test_no_cap_until_the_backend_refuses():
    assert ImageRejects().cap(_URL, "qwen3.6") is None


def test_a_cap_only_goes_down():
    rejects = ImageRejects()

    assert rejects.limit(_URL, "qwen3.6", 5) is True
    assert rejects.limit(_URL, "qwen3.6", 8) is False
    assert rejects.cap(_URL, "qwen3.6") == 5
    assert rejects.limit(_URL, "qwen3.6", 3) is True
    assert rejects.cap(_URL, "qwen3.6") == 3
    assert rejects.contains(_URL, "qwen3.6") is False  # потолок — не слепота


def test_blind_is_a_cap_of_zero():
    rejects = ImageRejects()
    rejects.limit(_URL, "qwen3.6", 2)

    assert rejects.mark(_URL, "qwen3.6") is True  # стала слепой только сейчас
    assert rejects.cap(_URL, "qwen3.6") == 0
    assert rejects.contains(_URL, "qwen3.6") is True
    assert rejects.mark(_URL, "qwen3.6") is False


def test_a_trial_view_does_not_touch_the_registry():
    """Пробный потолок шага — копия реестра с ещё одним потолком; реестр не меняется,
    пока успешный запрос его не подтвердит."""
    rejects = ImageRejects()
    rejects.limit(_URL, "qwen3.6", 4)

    trial = rejects.with_limit(_URL, "qwen3.6", 0)

    assert trial.contains(_URL, "qwen3.6") is True
    assert rejects.cap(_URL, "qwen3.6") == 4


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
    resolved = _resolved(_URL, "qwen3.6")

    assert (
        learn_image_cap(
            resolved,
            _http_error(
                400, "At most 3 image(s) may be provided in one prompt."
            ),
            fallback_url=_FALLBACK,
        )
        is False
    )
    assert process_image_rejects().cap(_URL, "qwen3.6") == 3
    blind = _http_error(
        400,
        "Model only supports text input; received unsupported content type 'image_url'",
    )
    assert (
        learn_image_cap(resolved, blind, fallback_url=_FALLBACK) is True
    )  # стала слепой
    assert learn_image_cap(resolved, blind, fallback_url=_FALLBACK) is False


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
    resolved = _resolved(_URL, "qwen3.6")

    assert (
        learn_image_cap(
            resolved, _http_error(status, message), fallback_url=_FALLBACK
        )
        is False
    )
    assert process_image_rejects().cap(_URL, "qwen3.6") is None


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
