"""«Модель не принимает изображения»: классификатор и три слоя повторов.

Спека изображений §10 и Review Focus №5 плана. Тексты — дословно из ответов
провайдеров (спека §10, исследование §7). Формы исключений — те, в которых их
поднимает НАШ транспорт: HTTP-отказ с уже прочитанным телом, SSE-ошибка внутри
200-стрима (собирает прод `StreamAssembler`), ошибка, зашитая прокси в HTTP-200
(собирает прод `_raise_on_embedded_choice_error`). Руками исключения с угаданной
формой не собираем: тест проверял бы догадку, а не транспорт.
"""

import json

import httpx
import pytest

from gc_core import request_backend_policy as policy
from gc_core.runtime_notices import collect_runtime_notices
from gc_core.utils import aretry
from gc_core.utils import connect_backend
from gc_core.utils.backend_errors import (
    EmbeddedChoiceError,
    image_limit_from_error,
    is_image_rejection_error,
    is_image_request_error,
    is_model_unavailable_error,
)
from gc_core.utils.chat_completions_stream import BackendStreamError
from gc_core.utils.stream_assembler import StreamAssembler
from gc_testkit import run_coro

from transport_fakes import install_transport, sse_body, stream_response

_URL = "https://backend.test/v1/chat/completions"

# --- Отказы «модель не видит изображения» (спека §10) -----------------------
VLLM_AT_MOST_0 = {
    "error": {
        "message": "At most 0 image(s) may be provided in one prompt. (parameter=image)",
        "type": "BadRequestError",
        "param": "image",
        "code": 400,
    }
}
VLLM_NOT_MULTIMODAL = {
    "object": "error",
    "message": "Qwen/Qwen3-Coder-Next is not a multimodal model",
    "type": "BadRequestError",
    "param": None,
    "code": 400,
}
OPENAI_BLIND = {
    "error": {
        "message": "Invalid content type. image_url is only supported by certain models.",
        "type": "invalid_request_error",
        "param": "messages.[1].content.[0].type",
        "code": None,
    }
}
OPENROUTER_404 = {
    "error": {
        "message": "No endpoints found that support image input",
        "code": 404,
    }
}
GATEWAY_NO_IMAGES = {
    "error": {
        "message": "Model does not support images. Please use a model that does.",
        "type": "invalid_request_error",
        "code": 400,
    }
}
SERDE_UNKNOWN_VARIANT = {
    "error": {
        "message": "Failed to deserialize the JSON body into the target type: messages[1]: "
        "unknown variant `image_url`, expected `text` at line 1 column 2051",
        "type": "invalid_request_error",
        "param": None,
        "code": "invalid_request_error",
    }
}
# Корпоративный litellm-прокси GigaCode (снято живьём 2026-09-24, модели gc_v2 и GLM-5.2-FP8).
LITELLM_TEXT_ONLY = {
    "error": {
        "message": "litellm.BadRequestError: OpenAIException - Model only supports text input; "
        "received unsupported content type 'image_url'.. Received Model Group=gc_v2\n"
        "Available Model Group Fallbacks=None",
        "type": None,
        "param": None,
        "code": "400",
    }
}

# --- Посторонние отказы запроса с картинками (Review Focus №5) --------------
VLLM_CONTEXT_OVERFLOW = {
    "error": {
        "message": "This model's maximum context length is 131072 tokens. However, you "
        "requested 133570 tokens in the messages. Please reduce the length of "
        "the messages.",
        "type": "BadRequestError",
        "param": None,
        "code": 400,
    }
}
OPENAI_INVALID_TOOL_SCHEMA = {
    "error": {
        "message": "Invalid schema for function 'fs_read_file': In context=(), 'required' is "
        "required to be supplied and to be an array including every key in "
        "properties. Missing 'path'.",
        "type": "invalid_request_error",
        "param": "tools[0].function.parameters",
        "code": "invalid_function_parameters",
    }
}
OPENAI_IMAGE_IN_TOOL_ROLE = {
    "error": {
        "message": "Invalid 'messages[3]'. Image URLs are only allowed for messages with "
        "role 'user', but this message with role 'tool' contains an image URL.",
        "type": "invalid_request_error",
        "param": "messages[3]",
        "code": None,
    }
}
NGINX_413 = "<html>\r\n<head><title>413 Request Entity Too Large</title></head>\r\n</html>\r\n"
OPENAI_REQUEST_TOO_LARGE = {
    "error": {
        "message": "Request too large for gpt-4o in organization org-test on tokens per min "
        "(TPM): Limit 30000, Requested 45000.",
        "type": "tokens",
        "param": None,
        "code": "rate_limit_exceeded",
    }
}


# --- Бэкенд принимает не все картинки запроса -------------------------------
# vLLM `--limit-mm-per-prompt`: тот же шаблон, что у отказа с нулём (снят живьём
# на ecs3 2026-09-22), с числом больше нуля.
VLLM_AT_MOST_5 = {
    "error": {
        "message": "At most 5 image(s) may be provided in one prompt. (parameter=image)",
        "type": "BadRequestError",
        "param": "image",
        "code": 400,
    }
}
# Синтетика: общая форма «слишком много картинок» без числа — у провайдеров свои
# тексты, путь без числа (деление пополам) от них не зависит.
SYNTHETIC_TOO_MANY_IMAGES = {
    "error": {
        "message": "Too many images in the request.",
        "type": "invalid_request_error",
    }
}
SYNTHETIC_NO_MORE_THAN = {
    "error": {
        "message": "You can send no more than 10 images per request.",
        "type": "invalid_request_error",
    }
}
# Переполнение контекста у мультимодальной модели vLLM (шаблон из исходников vLLM,
# не снят живьём): есть и «maximum», и «images», но предел — не картинок.
VLLM_DECODER_OVERFLOW = {
    "object": "error",
    "message": (
        "The decoder prompt (length 40211) is longer than the maximum model length of 32768. "
        "Make sure that `max_model_len` is no smaller than the number of text tokens plus "
        "multimodal tokens. For image inputs, the number of image tokens depends on the number "
        "of images, and possibly their aspect ratios as well."
    ),
    "type": "BadRequestError",
    "param": None,
    "code": 400,
}
# Синтетика: предел числа картинок без числа, другой порядок слов.
SYNTHETIC_IMAGES_EXCEED = {
    "error": {
        "message": "Number of images exceeds the limit for this model.",
        "type": "invalid_request_error",
    }
}
# Синтетика: ошибка схемы тула, где «image» — часть имени тула, а не слово, а
# «maximum» есть: не про картинки.
TOOL_SCHEMA_OF_VIEW_IMAGE = {
    "error": {
        "message": "Invalid schema for function 'fs_view_image': the maximum of 1 required "
        "property is exceeded.",
        "type": "invalid_request_error",
        "param": "tools[3]",
        "code": "invalid_function_parameters",
    }
}


def _http_error(status: int, body) -> httpx.HTTPStatusError:
    """HTTP-отказ в форме транспорта: тело прочитано до raise (стрим-путь
    дочитывает его в `_aread_error_text`, не-стрим получает сразу)."""
    request = httpx.Request("POST", _URL)
    if isinstance(body, str):
        response = httpx.Response(status, text=body, request=request)
    else:
        response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=response
    )


def _stream_error(error: dict) -> BackendStreamError:
    """SSE-ошибка внутри 200-стрима — собирает прод-сборщик стрима."""
    with pytest.raises(BackendStreamError) as caught:
        StreamAssembler().feed_payload(json.dumps({"error": error}))
    return caught.value


def _embedded_error(error: dict) -> EmbeddedChoiceError:
    """Ошибка, зашитая прокси в HTTP-200 (choices[0].error) — собирает прод."""
    with pytest.raises(EmbeddedChoiceError) as caught:
        policy._raise_on_embedded_choice_error({"choices": [{"error": error}]})
    return caught.value


@pytest.mark.parametrize(
    "status, body",
    [
        (400, VLLM_AT_MOST_0),
        (400, VLLM_NOT_MULTIMODAL),
        (400, OPENAI_BLIND),
        (404, OPENROUTER_404),
        (400, GATEWAY_NO_IMAGES),
        (422, SERDE_UNKNOWN_VARIANT),
        (400, LITELLM_TEXT_ONLY),
    ],
)
def test_http_rejection_is_recognised_by_its_body(status, body):
    assert is_image_rejection_error(_http_error(status, body)) is True


def test_stream_error_inside_200_is_recognised():
    assert (
        is_image_rejection_error(_stream_error(VLLM_AT_MOST_0["error"]))
        is True
    )


def test_error_embedded_by_proxy_is_recognised():
    embedded = _embedded_error(
        {"code": 404, "message": OPENROUTER_404["error"]["message"]}
    )
    assert is_image_rejection_error(embedded) is True


@pytest.mark.parametrize(
    "status, body",
    [
        (400, VLLM_CONTEXT_OVERFLOW),
        (400, OPENAI_INVALID_TOOL_SCHEMA),
        (400, OPENAI_IMAGE_IN_TOOL_ROLE),
        (413, NGINX_413),
        (429, OPENAI_REQUEST_TOO_LARGE),
        (
            404,
            {
                "error": {
                    "message": "model 'gpt-невидимка' not found",
                    "code": 404,
                }
            },
        ),
        (400, ""),
    ],
)
def test_unrelated_rejection_is_not_an_image_rejection(status, body):
    """Review Focus №5: посторонний 400 при запросе с картинками — не повод
    снимать картинки. Сюда же 413/«too large» (размер тела, путь — бюджет байт)
    и картинка не в user-сообщении (форма запроса — наш баг, не факт о модели)."""
    assert is_image_rejection_error(_http_error(status, body)) is False


def test_unrelated_stream_error_is_not_an_image_rejection():
    overflow = _stream_error(VLLM_CONTEXT_OVERFLOW["error"])
    assert is_image_rejection_error(overflow) is False


def test_http_rejection_with_unread_body_is_not_recognised():
    """Тело не дочитано (обрыв на чтении) — текста нет, классифицировать не по чему."""

    class _Unread(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b""

    request = httpx.Request("POST", _URL)
    response = httpx.Response(404, stream=_Unread(), request=request)
    exc = httpx.HTTPStatusError("HTTP 404", request=request, response=response)
    assert is_image_rejection_error(exc) is False


def test_only_backend_error_shapes_are_classified():
    """Текст отказа мог попасть в чужое исключение (причина предохранителя,
    ошибка хода) — классифицируется только сам отказ бэкенда."""
    text = VLLM_AT_MOST_0["error"]["message"]
    assert is_image_rejection_error(RuntimeError(text)) is False
    assert is_image_rejection_error(ValueError(text)) is False


# --- Картиночные ошибки запроса: отказ от зрения и предел числа картинок ----


@pytest.mark.parametrize(
    "status, body",
    [
        (400, VLLM_AT_MOST_0),
        (404, OPENROUTER_404),
        (400, LITELLM_TEXT_ONLY),
        (400, VLLM_AT_MOST_5),
        (400, SYNTHETIC_TOO_MANY_IMAGES),
        (422, SYNTHETIC_NO_MORE_THAN),
        (400, SYNTHETIC_IMAGES_EXCEED),
        (413, NGINX_413),
    ],
)
def test_image_request_errors_cover_blindness_and_limits(status, body):
    """Всё, что лечится меньшим числом картинок: слепота (0), предел с числом и без,
    тело запроса больше предела прокси (413)."""
    assert is_image_request_error(_http_error(status, body)) is True


@pytest.mark.parametrize(
    "status, body",
    [
        (400, VLLM_CONTEXT_OVERFLOW),
        (400, VLLM_DECODER_OVERFLOW),
        (400, OPENAI_INVALID_TOOL_SCHEMA),
        (400, TOOL_SCHEMA_OF_VIEW_IMAGE),
        (400, OPENAI_IMAGE_IN_TOOL_ROLE),
        (429, OPENAI_REQUEST_TOO_LARGE),
        (500, VLLM_AT_MOST_5),
        (400, ""),
    ],
)
def test_other_errors_are_not_image_request_errors(status, body):
    """Нужны и слово «image», и слово о пределе: имя тула `fs_view_image` и картинка не
    в той роли (форма запроса — наш баг) сюда не попадают; 5xx — не отказ запроса."""
    assert is_image_request_error(_http_error(status, body)) is False


def test_multimodal_context_overflow_is_an_overflow_not_an_image_limit():
    """Переполнение запускает сжатие истории; принять его за предел картинок — значит
    пометить зрячую модель слепой."""
    from gc_core.utils.backend_errors import is_context_overflow_error

    in_stream = _stream_error(
        {"message": VLLM_DECODER_OVERFLOW["message"], "code": 400}
    )
    assert is_context_overflow_error(in_stream) is True
    assert is_image_request_error(in_stream) is False
    assert (
        is_image_request_error(_http_error(400, VLLM_DECODER_OVERFLOW))
        is False
    )


def test_image_limit_errors_are_recognised_in_a_stream_and_embedded():
    assert (
        is_image_request_error(_stream_error(VLLM_AT_MOST_5["error"])) is True
    )
    embedded = _embedded_error(
        {"code": 400, "message": SYNTHETIC_TOO_MANY_IMAGES["error"]["message"]}
    )
    assert is_image_request_error(embedded) is True


@pytest.mark.parametrize(
    "status, body, limit",
    [
        (400, VLLM_AT_MOST_5, 5),
        (400, VLLM_AT_MOST_0, 0),
        (422, SYNTHETIC_NO_MORE_THAN, 10),
        (400, SYNTHETIC_TOO_MANY_IMAGES, None),
        (413, NGINX_413, None),
        (400, TOOL_SCHEMA_OF_VIEW_IMAGE, None),
    ],
)
def test_the_stated_limit_is_read_from_the_error(status, body, limit):
    assert image_limit_from_error(_http_error(status, body)) == limit


# --- Слой 3: предохранитель -------------------------------------------------


def test_image_rejection_is_not_model_unavailability():
    """OpenRouter отвечает слепой модели 404-м: без классификатора алиас гас бы
    на всю сессию, а основной путь поднимал бы «модель недоступна»."""
    assert (
        is_model_unavailable_error(_http_error(404, OPENROUTER_404)) is False
    )
    embedded = _embedded_error(
        {"code": 404, "message": OPENROUTER_404["error"]["message"]}
    )
    assert is_model_unavailable_error(embedded) is False
    # Обычный 404 — по-прежнему недоступность (переименованная или снятая модель).
    assert (
        is_model_unavailable_error(
            _http_error(
                404, {"error": {"message": "model 'gpt-невидимка' not found"}}
            )
        )
        is True
    )


# --- Слой 1: транспорт ------------------------------------------------------


def _record_sleeps(monkeypatch) -> list:
    delays: list = []

    async def _fake_sleep(delay, *_a, **_k):
        delays.append(delay)

    monkeypatch.setattr(aretry.asyncio, "sleep", _fake_sleep)
    return delays


def test_transport_predicate_does_not_retry_image_rejection():
    assert (
        connect_backend._default_should_retry_backend_exception(
            _http_error(404, OPENROUTER_404)
        )
        is False
    )
    assert (
        connect_backend._default_should_retry_backend_exception(
            _stream_error(VLLM_AT_MOST_0["error"])
        )
        is False
    )
    # Прочий 404 и прочая стрим-ошибка ретраятся, как раньше.
    assert (
        connect_backend._default_should_retry_backend_exception(
            _http_error(404, {"error": {"message": "route not ready"}})
        )
        is True
    )
    assert (
        connect_backend._default_should_retry_backend_exception(
            _stream_error(VLLM_CONTEXT_OVERFLOW["error"])
        )
        is True
    )


def test_transport_sends_openrouter_image_404_once(monkeypatch):
    """Стрим-путь: тело 404 дочитано до raise, поэтому классификатор его видит,
    и транспорт не ждёт 10/20/40/80 с на отказе, который повтором не лечится."""
    spy = install_transport(
        monkeypatch, lambda request: httpx.Response(404, json=OPENROUTER_404)
    )
    delays = _record_sleeps(monkeypatch)

    with pytest.raises(httpx.HTTPStatusError) as caught:
        run_coro(
            connect_backend.arequest_backend_chat_completions(
                {"model": "m", "messages": []},
                {},
                url=_URL,
                use_retry=True,
                on_delta=lambda kind, text: None,
            )
        )

    assert is_image_rejection_error(caught.value) is True
    assert len(spy.requests) == 1
    assert delays == []


def test_transport_sends_stream_image_error_once(monkeypatch):
    """SSE-ошибка внутри 200-стрима — подкласс ValueError, и прежде транспорт
    повторял её как «оборванный стрим»."""
    spy = install_transport(
        monkeypatch,
        stream_response(sse_body({"error": VLLM_AT_MOST_0["error"]})),
    )
    delays = _record_sleeps(monkeypatch)

    with pytest.raises(BackendStreamError):
        run_coro(
            connect_backend.arequest_backend_chat_completions(
                {"model": "m", "messages": []},
                {},
                url=_URL,
                use_retry=True,
                on_delta=lambda kind, text: None,
            )
        )

    assert len(spy.requests) == 1
    assert delays == []


# --- Слой 2: адаптивная политика --------------------------------------------


def _patch_policy_transport(monkeypatch, outcomes: list) -> dict:
    """Транспорт политики отвечает по очереди: исключение поднимается, словарь
    возвращается. Паузы политики — без ожидания."""

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(policy.asyncio, "sleep", _no_sleep)
    calls = {"n": 0}

    async def _fake_request(
        payload,
        headers,
        url,
        verify,
        use_retry,
        logger,
        on_delta,
        cert=None,
        timeout=600,
        config_proxy=None,
    ):
        outcome = outcomes[min(calls["n"], len(outcomes) - 1)]
        calls["n"] += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(
        policy, "arequest_backend_chat_completions", _fake_request
    )
    return calls


#: Сообщение с картинкой, как его собирает проекция: картиночная ошибка что-то значит,
#: только если в запросе были картинки.
_IMAGE_MESSAGE = {
    "role": "user",
    "content": [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
        },
        {"type": "text", "text": "что тут?"},
    ],
}


def _run_policy(*, images: bool = True):
    messages = (
        [_IMAGE_MESSAGE]
        if images
        else [{"role": "user", "content": "что тут?"}]
    )
    return run_coro(
        policy.arequest_chat_completions_with_policy(
            payload_factory=lambda max_tokens: {
                "messages": messages,
                "max_tokens": max_tokens,
            },
            initial_max_tokens=8192,
            min_response_tokens=1024,
            headers={},
            url=_URL,
            verify=True,
            use_retry=False,
            request_label="main_agent",
            runtime_notices_enabled=True,
            notice_url=_URL,
        )
    )


def test_policy_raises_image_rejection_at_once_without_notice(monkeypatch):
    rejection = _http_error(400, VLLM_AT_MOST_0)
    calls = _patch_policy_transport(monkeypatch, [rejection])

    with pytest.raises(httpx.HTTPStatusError) as caught:
        _run_policy()

    assert caught.value is rejection
    assert calls["n"] == 1
    assert collect_runtime_notices() == []


def test_policy_stops_adaptive_retries_on_image_rejection(monkeypatch):
    """Первый отказ транзиентный (500), повтор упёрся в слепоту модели: дальше
    понижать max_tokens бессмысленно, наверх уходит сам картиночный отказ —
    по нему контроллер снимет картинки."""
    rejection = _http_error(400, VLLM_AT_MOST_0)
    calls = _patch_policy_transport(
        monkeypatch, [_http_error(500, "upstream"), rejection]
    )

    with pytest.raises(httpx.HTTPStatusError) as caught:
        _run_policy()

    assert caught.value is rejection
    assert calls["n"] == 2
    assert collect_runtime_notices() == []


@pytest.mark.parametrize(
    "status, body",
    [(400, VLLM_AT_MOST_0), (404, OPENROUTER_404)],
    ids=["vllm-400", "openrouter-404"],
)
@pytest.mark.parametrize(
    "streaming", [True, False], ids=["stream", "no-stream"]
)
def test_policy_over_the_real_transport_sends_an_image_rejection_once(
    monkeypatch,
    status,
    body,
    streaming,
):
    """Слои 1 и 2 вместе, как их проходит основной путь: одна HTTP-попытка, ни
    одной паузы, без «Ошибки LLM». Без слоёв vLLM-400 уходил шесть раз с
    паузами политики, а 404 OpenRouter ждал 10/20/40/80 с транспорта."""
    spy = install_transport(
        monkeypatch, lambda request: httpx.Response(status, json=body)
    )
    delays = _record_sleeps(monkeypatch)

    with pytest.raises(httpx.HTTPStatusError) as caught:
        run_coro(
            policy.arequest_chat_completions_with_policy(
                payload_factory=lambda max_tokens: {
                    "model": "m",
                    "messages": [_IMAGE_MESSAGE],
                    "max_tokens": max_tokens,
                },
                initial_max_tokens=8192,
                min_response_tokens=1024,
                headers={},
                url=_URL,
                verify=True,
                use_retry=True,
                on_delta=(lambda kind, text: None) if streaming else None,
                request_label="main_agent",
                runtime_notices_enabled=True,
                notice_url=_URL,
            )
        )

    assert is_image_rejection_error(caught.value) is True
    assert len(spy.requests) == 1
    assert delays == []
    assert collect_runtime_notices() == []


@pytest.mark.parametrize(
    "status, body",
    [(400, VLLM_AT_MOST_5), (413, NGINX_413)],
    ids=["vllm-limit", "proxy-413"],
)
def test_policy_raises_an_image_limit_error_at_once_without_notice(
    monkeypatch, status, body
):
    """Предел числа картинок понижением max_tokens не лечится: наверх сразу, картинки
    урежет контроллер."""
    rejection = _http_error(status, body)
    calls = _patch_policy_transport(monkeypatch, [rejection])

    with pytest.raises(httpx.HTTPStatusError) as caught:
        _run_policy()

    assert caught.value is rejection
    assert calls["n"] == 1
    assert collect_runtime_notices() == []


def test_policy_keeps_the_old_path_for_a_413_without_images(monkeypatch):
    """Служебный вызов без картинок упёрся в предел тела прокси: картинки ни при чём —
    прежний путь, с «Ошибкой LLM» в уведомлениях."""
    calls = _patch_policy_transport(monkeypatch, [_http_error(413, NGINX_413)])

    with pytest.raises(httpx.HTTPStatusError):
        _run_policy(images=False)

    assert calls["n"] == 6  # первая попытка + 5 адаптивных
    assert [n.code for n in collect_runtime_notices()] == ["llm_error_413"]


def test_policy_keeps_the_old_path_for_an_unrelated_400(monkeypatch):
    """Review Focus №5 на уровне политики: битая схема тула в запросе с
    картинками идёт прежним путём — адаптивные повторы и «Ошибка LLM»."""
    calls = _patch_policy_transport(
        monkeypatch, [_http_error(400, OPENAI_INVALID_TOOL_SCHEMA)]
    )

    with pytest.raises(httpx.HTTPStatusError):
        _run_policy()

    assert calls["n"] == 6  # первая попытка + 5 адаптивных
    notices = collect_runtime_notices()
    assert [n.code for n in notices] == ["llm_error_400"]
