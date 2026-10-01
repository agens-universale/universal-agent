"""Картиночная ошибка запроса (спека §10): отказ модели от картинок или предел их числа.
Повтор в том же шаге с меньшим числом картинок (0 — записки). Явный факт — отказ от
зрения, названный предел — запоминается на процесс сразу; успешный выведенный потолок
(413, «слишком много» без числа) сохраняется для своего хода и контекста. Пометка в ленте —
только когда модель впервые стала слепой и смотреть больше некому. Запрос без картинок
идёт прежним путём.

Транспорт настоящий (``connect_backend`` + политика повторов) поверх фейкового
HTTP: заодно видно, что ни транспорт, ни адаптивная политика картиночную ошибку
не повторяют — запросов ровно два.
"""

import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from gc_core import processing_responses as pr
from gc_core import request_backend_policy as policy
from gc_core.attachments.model import ORIGIN_USER, user_label, wh_text
from gc_core.attachments.normalize import NormalizedImage
from gc_core.attachments.store import ImageStore
from gc_core.attachments.texts import STUB_MODEL_BLIND, STUB_NOBODY
from gc_core.constants.tool_names import FS_VIEW_IMAGE_NAME
from gc_core.model_router import ModelRouter
from gc_core.vision.notes import NOTE_MAIN_BLIND
from gc_core.vision.rejects import (
    process_image_rejects,
    reset_image_rejects_for_tests,
)
from transport_fakes import install_transport
from gc_testkit import run_coro

URL = "http://llm.test/v1/chat/completions"
#: Тело отказа vLLM у зрячей модели, поднятой без зрения (спека §10, `--language-model-only`).
VLLM_NO_IMAGES = {
    "object": "error",
    "message": "At most 0 image(s) may be provided in one prompt.",
    "type": "BadRequestError",
    "param": None,
    "code": 400,
}
OK = {
    "id": "r1",
    "choices": [
        {
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
}
VIEW_TOOL = {
    "type": "function",
    "name": FS_VIEW_IMAGE_NAME,
    "description": "view",
    "parameters": {"type": "object", "properties": {}},
}


@pytest.fixture(autouse=True)
def _fresh_rejects():
    reset_image_rejects_for_tests()
    yield
    reset_image_rejects_for_tests()


def _rejects_images(request: httpx.Request) -> httpx.Response:
    """Модель без зрения: запрос с картинкой — 400 vLLM, без картинки — ответ."""
    if b"image_url" in request.content:
        return httpx.Response(400, json=VLLM_NO_IMAGES)
    return httpx.Response(200, json=OK)


def _cfg(**extra) -> dict:
    return {
        "model": "blind-model",
        "completions_url": URL,
        "backend_api_mode": "chat_completions",
        "chat_completions_max_tokens": 256,
        "chat_completions_min_response_tokens": 64,
        "backend_request_timeout_sec": 30,
        "use_ssl": False,
        "use_retry": False,
        "x_cl_id": "test-cl-id",
        **extra,
    }


def _ctx(tmp_path, cfg: dict, notes: list) -> SimpleNamespace:
    return SimpleNamespace(
        config_dict=cfg,
        on_stream_delta=None,
        logger=logging.getLogger("test_image_rejection_retry"),
        auth_connector=None,
        is_tui=False,
        model_router=ModelRouter(cfg, level_provider=lambda: "default"),
        image_store=ImageStore(str(tmp_path)),
        on_trace_note=notes.append,
    )


def _state(
    messages: list[dict], tools: list[dict] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        state_id="reject-state",
        x_target=None,
        messages=messages,
        tools=list(tools or []),
        default_model="blind-model",
        usage=None,
    )


def _user_with_image(store: ImageStore):
    att = store.stage(
        NormalizedImage(
            data=b"\x89PNG reject test",
            mime="image/png",
            width=40,
            height=30,
            orig_width=40,
            orig_height=30,
        ),
        origin=ORIGIN_USER,
        label=user_label(1),
        source="clipboard",
    )
    return att, {
        "role": "user",
        "id": "m1",
        "content": "[Изображение #1] что тут?",
        "attachments": [att.to_dict()],
    }


def _users(request: httpx.Request) -> list[dict]:
    return [
        m
        for m in json.loads(request.content)["messages"]
        if m["role"] == "user"
    ]


def test_image_rejection_marks_the_model_and_retries_once_without_images(
    tmp_path, monkeypatch
):
    """Легаси-реестр: «глаза» — та же модель, после пометки смотреть некому → пометка в ленте."""
    spy = install_transport(monkeypatch, _rejects_images)
    notes: list = []
    ctx = _ctx(tmp_path, _cfg(), notes)
    att, message = _user_with_image(ctx.image_store)
    state = _state([message])

    run_coro(pr._arequest_backend_canonical([message], state, ctx))

    assert len(spy.requests) == 2
    assert b"image_url" in spy.requests[0].content
    assert _users(spy.requests[1]) == [
        {
            "role": "user",
            "content": STUB_NOBODY.format(
                label=att.label, wh=wh_text(att), path=att.path
            )
            + "\n[Изображение #1] что тут?",
        }
    ]
    assert process_image_rejects().contains(URL, "blind-model") is True
    assert notes == [NOTE_MAIN_BLIND.format(alias="blind-model")]
    assert state.vision_audience.nobody is True


def test_with_assigned_eyes_the_retry_points_to_the_view_tool_and_stays_silent(
    tmp_path, monkeypatch
):
    """`assign.vision` на другую модель: после пометки смотрят «глаза» — записка
    «модель не видит», тул просмотра в наборе, пометки в ленте нет."""
    spy = install_transport(monkeypatch, _rejects_images)
    notes: list = []
    cfg = _cfg(
        models={
            "blind": {"model": "blind-model"},
            "eye": {"model": "eye-model"},
        },
        slots={"default": "blind"},
        assign={"vision": "eye"},
    )
    ctx = _ctx(tmp_path, cfg, notes)
    att, message = _user_with_image(ctx.image_store)
    state = _state([message], tools=[VIEW_TOOL])

    run_coro(pr._arequest_backend_canonical([message], state, ctx))

    assert len(spy.requests) == 2
    retry = json.loads(spy.requests[1].content)
    assert _users(spy.requests[1]) == [
        {
            "role": "user",
            "content": STUB_MODEL_BLIND.format(
                label=att.label, wh=wh_text(att), path=att.path
            )
            + "\n[Изображение #1] что тут?",
        }
    ]
    assert [tool["function"]["name"] for tool in retry["tools"]] == [
        FS_VIEW_IMAGE_NAME
    ]
    assert notes == []


def test_request_without_images_keeps_the_old_path(tmp_path, monkeypatch):
    """Картинок в запросе нет — та же ошибка уходит наверх как была: без пометки и повтора."""
    spy = install_transport(
        monkeypatch, lambda request: httpx.Response(400, json=VLLM_NO_IMAGES)
    )
    notes: list = []
    ctx = _ctx(tmp_path, _cfg(), notes)
    state = _state([{"role": "user", "id": "m1", "content": "просто текст"}])

    with pytest.raises(httpx.HTTPStatusError):
        run_coro(pr._arequest_backend_canonical(state.messages, state, ctx))

    assert len(spy.requests) == 1
    assert process_image_rejects().contains(URL, "blind-model") is False
    assert notes == []


#: Посторонние отказы запроса с картинками — дословно, как в test_image_rejection_error.py.
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
OWN_URL = "http://own.test/v1/chat/completions"


def _user_texts(request: httpx.Request) -> str:
    """Все текстовые части user-сообщений запроса — здесь видны записки на месте картинок."""
    parts = []
    for message in _users(request):
        content = message["content"]
        if isinstance(content, str):
            parts.append(content)
        else:
            parts.extend(
                part["text"] for part in content if part.get("type") == "text"
            )
    return "\n".join(parts)


@pytest.mark.parametrize(
    "body", [VLLM_CONTEXT_OVERFLOW, OPENAI_INVALID_TOOL_SCHEMA]
)
def test_unrelated_400_with_live_images_goes_up_without_mark_or_retry(
    tmp_path, monkeypatch, body
):
    """Review Focus №5 на уровне контроллера: посторонний 400 запроса с живой
    картинкой — не повод снимать картинки. Контроллер не повторяет: запросы —
    только первая попытка и адаптивные понижения max_tokens политики (прежний
    путь любого 4xx), картинка во всех пикселями, пометки и записок нет, ошибка
    уходит наверх как была."""
    monkeypatch.setattr(policy, "_CHAT_COMPLETIONS_HTTP_RETRY_DELAY_SEC", 0)
    spy = install_transport(
        monkeypatch, lambda request: httpx.Response(400, json=body)
    )
    notes: list = []
    ctx = _ctx(tmp_path, _cfg(), notes)
    _att, message = _user_with_image(ctx.image_store)
    state = _state([message])

    with pytest.raises(httpx.HTTPStatusError) as caught:
        run_coro(pr._arequest_backend_canonical([message], state, ctx))

    assert caught.value.response.json() == body
    assert (
        len(spy.requests)
        == 1 + policy._CHAT_COMPLETIONS_ADAPTIVE_MAX_TOKEN_STEPS
    )
    assert all(b"image_url" in request.content for request in spy.requests)
    assert all("·" not in _user_texts(request) for request in spy.requests)
    assert process_image_rejects().contains(URL, "blind-model") is False
    assert notes == []


def test_model_with_its_own_url_is_marked_by_that_url(tmp_path, monkeypatch):
    """Пометка — по адресу, куда ушёл запрос (`request_url`): у модели реестра он свой,
    не `completions_url`. Повтор — без картинки."""
    spy = install_transport(monkeypatch, _rejects_images)
    notes: list = []
    cfg = _cfg(
        models={"blind": {"url": OWN_URL, "model": "blind-model"}},
        slots={"default": "blind"},
    )
    ctx = _ctx(tmp_path, cfg, notes)
    _att, message = _user_with_image(ctx.image_store)
    state = _state([message])

    run_coro(pr._arequest_backend_canonical([message], state, ctx))

    assert [str(request.url) for request in spy.requests] == [OWN_URL, OWN_URL]
    assert b"image_url" in spy.requests[0].content
    assert b"image_url" not in spy.requests[1].content
    assert process_image_rejects().contains(OWN_URL, "blind-model") is True
    assert process_image_rejects().contains(URL, "blind-model") is False


def test_failed_retry_goes_up_and_is_not_retried_again(tmp_path, monkeypatch):
    """Повтор без картинок ровно один: упал и он (500) — ошибка уходит наверх.
    Картинка — только в первом запросе; дальше — адаптивные понижения max_tokens
    политики внутри того же повтора, третьего захода контроллера нет."""
    monkeypatch.setattr(policy, "_CHAT_COMPLETIONS_HTTP_RETRY_DELAY_SEC", 0)

    def _reject_then_fail(request: httpx.Request) -> httpx.Response:
        if b"image_url" in request.content:
            return httpx.Response(400, json=VLLM_NO_IMAGES)
        return httpx.Response(
            500, json={"error": {"message": "Internal Server Error"}}
        )

    spy = install_transport(monkeypatch, _reject_then_fail)
    notes: list = []
    ctx = _ctx(tmp_path, _cfg(), notes)
    _att, message = _user_with_image(ctx.image_store)
    state = _state([message])

    with pytest.raises(httpx.HTTPStatusError) as caught:
        run_coro(pr._arequest_backend_canonical([message], state, ctx))

    assert caught.value.response.status_code == 500
    retry_attempts = 1 + policy._CHAT_COMPLETIONS_ADAPTIVE_MAX_TOKEN_STEPS
    assert [b"image_url" in request.content for request in spy.requests] == [
        True
    ] + [False] * retry_attempts
    # Отказ от зрения — факт о модели: запомнен, хотя повтор упал по другой причине.
    assert process_image_rejects().contains(URL, "blind-model") is True


def test_no_trace_note_when_a_parallel_request_marked_the_model_first(
    tmp_path, monkeypatch
):
    """Пометку поставил параллельный запрос (субагент) между сборкой этого запроса и
    отказом: пометка не новая — строку в ленте уже показал её владелец, повтор молчит."""

    def _marked_meanwhile(request: httpx.Request) -> httpx.Response:
        if b"image_url" in request.content:
            process_image_rejects().mark(URL, "blind-model")
            return httpx.Response(400, json=VLLM_NO_IMAGES)
        return httpx.Response(200, json=OK)

    spy = install_transport(monkeypatch, _marked_meanwhile)
    notes: list = []
    ctx = _ctx(tmp_path, _cfg(), notes)
    _att, message = _user_with_image(ctx.image_store)
    state = _state([message])

    run_coro(pr._arequest_backend_canonical([message], state, ctx))

    assert len(spy.requests) == 2
    assert state.vision_audience.nobody is True
    assert notes == []


# --- Предел числа картинок -----------------------------------------------------

VLLM_AT_MOST_1 = {
    "object": "error",
    "message": "At most 1 image(s) may be provided in one prompt.",
    "type": "BadRequestError",
    "param": None,
    "code": 400,
}
NGINX_413 = (
    "<html><head><title>413 Request Entity Too Large</title></head></html>"
)


def _user_with_images(store: ImageStore, count: int):
    atts = [
        store.stage(
            NormalizedImage(
                data=f"\x89PNG limit test {n}".encode(),
                mime="image/png",
                width=40,
                height=30,
                orig_width=40,
                orig_height=30,
            ),
            origin=ORIGIN_USER,
            label=user_label(n),
            source="clipboard",
        )
        for n in range(1, count + 1)
    ]
    text = (
        " ".join(f"[Изображение #{n}]" for n in range(1, count + 1))
        + " что тут?"
    )
    return atts, {
        "role": "user",
        "id": "m1",
        "content": text,
        "attachments": [a.to_dict() for a in atts],
    }


def _image_parts(request: httpx.Request) -> int:
    return sum(
        1
        for message in _users(request)
        if isinstance(message["content"], list)
        for part in message["content"]
        if part.get("type") == "image_url"
    )


def _refuse_over(limit: int, response):
    """Бэкенд с пределом: больше `limit` картинок — отказ `response()`, иначе ответ."""

    def handler(request: httpx.Request) -> httpx.Response:
        return (
            response()
            if _image_parts(request) > limit
            else httpx.Response(200, json=OK)
        )

    return handler


def test_a_stated_limit_keeps_the_newest_images_and_is_remembered(
    tmp_path, monkeypatch
):
    spy = install_transport(
        monkeypatch,
        _refuse_over(1, lambda: httpx.Response(400, json=VLLM_AT_MOST_1)),
    )
    notes: list = []
    ctx = _ctx(tmp_path, _cfg(), notes)
    atts, message = _user_with_images(ctx.image_store, 3)
    state = _state([message])

    run_coro(pr._arequest_backend_canonical([message], state, ctx))

    assert [_image_parts(request) for request in spy.requests] == [3, 1]
    assert f"{atts[0].label} · " in _user_texts(
        spy.requests[1]
    )  # старые — записками
    assert f"{atts[2].label} · " not in _user_texts(
        spy.requests[1]
    )  # новая — картинкой
    assert process_image_rejects().cap(URL, "blind-model") == 1
    assert notes == []

    run_coro(
        pr._arequest_backend_canonical([message], state, ctx)
    )  # следующий шаг
    assert _image_parts(spy.requests[-1]) == 1
    assert len(spy.requests) == 3


def test_a_limit_without_a_number_halves_until_the_request_fits(
    tmp_path, monkeypatch
):
    spy = install_transport(
        monkeypatch,
        _refuse_over(1, lambda: httpx.Response(413, text=NGINX_413)),
    )
    notes: list = []
    ctx = _ctx(tmp_path, _cfg(), notes)
    _atts, message = _user_with_images(ctx.image_store, 4)
    state = _state([message])

    run_coro(pr._arequest_backend_canonical([message], state, ctx))

    assert [_image_parts(request) for request in spy.requests] == [4, 2, 1]
    assert (
        process_image_rejects().cap(URL, "blind-model") is None
    )  # 413 — про объём, не про модель
    assert notes == []

    run_coro(pr._arequest_backend_canonical([message], state, ctx))
    assert [_image_parts(request) for request in spy.requests[3:]] == [1]


@pytest.mark.parametrize(
    "reset", ["turn", "history", "images", "model", "url"]
)
def test_an_inferred_limit_is_rechecked_after_its_scope_changes(
    tmp_path, monkeypatch, reset
):
    spy = install_transport(
        monkeypatch,
        _refuse_over(1, lambda: httpx.Response(413, text=NGINX_413)),
    )
    ctx = _ctx(tmp_path, _cfg(), [])
    _atts, message = _user_with_images(ctx.image_store, 4)
    state = _state([message])
    run_coro(pr._arequest_backend_canonical([message], state, ctx))
    run_coro(pr._arequest_backend_canonical([message], state, ctx))
    assert [_image_parts(request) for request in spy.requests[3:]] == [1]

    if reset == "turn":
        state.user_turn_seq = 1
    elif reset == "history":
        state.dialog_steps_offset = 1
    elif reset == "images":
        _atts, message = _user_with_images(ctx.image_store, 5)
        state.messages = [message]
    elif reset == "model":
        ctx.model_router = ModelRouter(
            _cfg(model="another-model"), level_provider=lambda: "default"
        )
    else:
        ctx.model_router = ModelRouter(
            _cfg(completions_url=URL + "/other"),
            level_provider=lambda: "default",
        )
    start = len(spy.requests)
    run_coro(pr._arequest_backend_canonical([message], state, ctx))

    expected = [5, 2, 1] if reset == "images" else [4, 2, 1]
    assert [_image_parts(request) for request in spy.requests[start:]] == (
        expected
    )


def test_a_proxy_that_takes_no_image_goes_with_notes_without_blaming_the_model(
    tmp_path, monkeypatch
):
    """Даже одна картинка не пролезла через предел тела прокси, а запрос без картинок
    прошёл: этот запрос уходит записками. Модель не помечается и строки «не работает с
    изображениями» нет — дело в объёме запроса, после сжатия истории картинка пролезет."""
    spy = install_transport(
        monkeypatch,
        _refuse_over(0, lambda: httpx.Response(413, text=NGINX_413)),
    )
    notes: list = []
    ctx = _ctx(tmp_path, _cfg(), notes)
    _atts, message = _user_with_images(ctx.image_store, 2)
    state = _state([message])

    run_coro(pr._arequest_backend_canonical([message], state, ctx))

    assert [_image_parts(request) for request in spy.requests] == [2, 1, 0]
    assert process_image_rejects().cap(URL, "blind-model") is None
    assert notes == []

    run_coro(pr._arequest_backend_canonical([message], state, ctx))

    assert [_image_parts(request) for request in spy.requests[3:]] == [0]
    assert process_image_rejects().contains(URL, "blind-model") is False


def test_a_request_that_fails_even_without_images_raises_its_error(
    tmp_path, monkeypatch
):
    """413 и без картинок — дело не в них (длинная история): запрос без картинок идёт
    прежним путём любой 4xx (адаптивные понижения max_tokens политики), модель не
    помечается, наверх уходит его ошибка."""
    monkeypatch.setattr(policy, "_CHAT_COMPLETIONS_HTTP_RETRY_DELAY_SEC", 0)
    spy = install_transport(
        monkeypatch, lambda request: httpx.Response(413, text=NGINX_413)
    )
    notes: list = []
    ctx = _ctx(tmp_path, _cfg(), notes)
    _atts, message = _user_with_images(ctx.image_store, 2)
    state = _state([message])

    with pytest.raises(httpx.HTTPStatusError) as caught:
        run_coro(pr._arequest_backend_canonical([message], state, ctx))

    assert caught.value.response.status_code == 413
    retry_attempts = 1 + policy._CHAT_COMPLETIONS_ADAPTIVE_MAX_TOKEN_STEPS
    assert [_image_parts(request) for request in spy.requests] == [2, 1] + [
        0
    ] * retry_attempts
    assert process_image_rejects().cap(URL, "blind-model") is None
    assert notes == []
    assert getattr(state, "vision_request_limit", None) is None
