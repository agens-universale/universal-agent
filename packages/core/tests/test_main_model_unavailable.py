from types import SimpleNamespace

import pytest
from gc_core import processing_responses as pr
from gc_core.execution.contracts import MainModelUnavailable
from gc_testkit.settings import fake_ctx


def test_the_honest_main_error_is_a_typed_runtime_error():
    resolved = SimpleNamespace(
        alias="mid", model="mid-model", url="http://mid/v1/chat/completions"
    )
    with pytest.raises(MainModelUnavailable) as caught:
        pr._raise_honest_main_unavailable(
            SimpleNamespace(is_tui=False), resolved, RuntimeError("503")
        )
    assert isinstance(caught.value, RuntimeError)
    assert "mid-model" in str(caught.value)


def _http_error(status: int, url: str):
    import httpx

    request = httpx.Request("POST", url)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(
        f"Client error '{status}'", request=request, response=response
    )


def _honest_message(
    status: int, url: str, api_mode: str = "chat_completions"
) -> str:
    resolved = SimpleNamespace(alias="r", model="m", url=url)
    exc = _http_error(status, url)
    ctx = fake_ctx({"backend.api_mode": api_mode}, is_tui=False)
    with pytest.raises(MainModelUnavailable) as caught:
        pr._raise_honest_main_unavailable(ctx, resolved, exc)
    return str(caught.value)


def test_404_on_address_without_endpoint_path_hints_the_path():
    """Адрес без пути эндпоинта, 404 — ошибка подсказывает проверить
    адрес, а не только сменить модель."""
    text = _honest_message(404, "https://routerai.ru/api/v1")

    assert "проверьте адрес" in text
    assert "/chat/completions" in text


def test_5xx_and_404_on_full_address_have_no_address_hint():
    assert "проверьте адрес" not in _honest_message(
        503, "https://routerai.ru/api/v1"
    )
    assert "проверьте адрес" not in _honest_message(
        404, "https://h/v1/chat/completions"
    )


def test_404_hint_in_responses_mode_names_the_responses_path():
    text = _honest_message(404, "https://h/v1", api_mode="responses")

    assert "проверьте адрес" in text
    assert "/responses" in text
    assert "/chat/completions" not in text


def test_404_hint_judges_the_path_of_the_current_mode():
    assert "проверьте адрес" not in _honest_message(
        404, "https://h/v1/responses", api_mode="responses"
    )
    assert "проверьте адрес" in _honest_message(
        404, "https://h/v1/chat/completions", api_mode="responses"
    )
