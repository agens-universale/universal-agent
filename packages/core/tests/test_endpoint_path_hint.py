"""Подсказка полного адреса модели: адрес без пути эндпоинта режима
(`/chat/completions`, `/responses`) принимается, но с предупреждением."""

from gc_core.config_modules.backend import endpoint_path_hint


def test_base_address_gets_chat_completions_hint():
    assert (
        endpoint_path_hint("https://routerai.ru/api/v1", "chat_completions")
        == "https://routerai.ru/api/v1/chat/completions"
    )


def test_trailing_slash_is_not_doubled():
    assert (
        endpoint_path_hint("https://h/v1/", "chat_completions")
        == "https://h/v1/chat/completions"
    )


def test_full_address_needs_no_hint():
    assert (
        endpoint_path_hint("https://h/v1/chat/completions", "chat_completions")
        is None
    )


def test_responses_mode_expects_responses_path():
    assert endpoint_path_hint("https://h/v1", "responses") == (
        "https://h/v1/responses"
    )
    assert endpoint_path_hint("https://h/v1/responses", "responses") is None


def test_env_reference_and_empty_address_are_not_judged():
    assert endpoint_path_hint("${ENV:LLM_URL}", "chat_completions") is None
    assert endpoint_path_hint("", "chat_completions") is None
