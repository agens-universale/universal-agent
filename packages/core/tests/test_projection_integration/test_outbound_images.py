"""Проекция вложений в запросе главного цикла (спека §5, §9.2): живые картинки уходят
частями ``image_url``, остальные — записками; ``fs_view_image`` виден, только когда
есть кому смотреть; в ``responses`` — только записки и без ключа ``attachments``."""

from __future__ import annotations

import base64
import copy
import logging
from types import SimpleNamespace

import pytest

from gc_core import processing_responses as pr
from gc_core.attachments.model import ORIGIN_USER, user_label, wh_text
from gc_core.attachments.normalize import NormalizedImage
from gc_core.attachments.store import ImageStore
from gc_core.attachments.texts import (
    STUB_MODEL_BLIND,
    STUB_MODEL_REJECTS_NOBODY,
    STUB_NOBODY,
    STUB_PROBE_FAILED,
    STUB_OUT_OF_CONTEXT,
)
from gc_core.constants.tool_names import FS_VIEW_IMAGE_NAME
from gc_core.model_router import ModelRouter
from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx, make_settings

from vision_fakes import (
    clear_image_rejects,
    route_key,
    unsupported,
    warm_routes,
)
from gc_core.vision.rejects import mark_blind

URL = "http://test-host/v1/chat/completions"
MODEL = "vis-model"
PNG = b"\x89PNG outbound test bytes"
VIEW_TOOL = {
    "type": "function",
    "name": FS_VIEW_IMAGE_NAME,
    "description": "view",
    "parameters": {"type": "object", "properties": {}},
}
READ_TOOL = {
    "type": "function",
    "name": "fs_read_file",
    "description": "read",
    "parameters": {"type": "object", "properties": {}},
}


@pytest.fixture(autouse=True)
def _fresh_rejects():
    clear_image_rejects()
    yield
    clear_image_rejects()


@pytest.fixture
def capture_responses(monkeypatch):
    """Мок транспорта responses: снимает call_data и отвечает пустым выводом."""
    captured: dict = {}

    async def fake_responses(call_data, headers, **_kwargs):
        captured["call_data"] = call_data
        return {"id": "resp_1", "output": []}, {}

    monkeypatch.setattr(pr, "arequest_backend", fake_responses)
    return captured


def _cfg(mode: str = "chat_completions") -> dict:
    return {
        "agent.model": MODEL,
        # Адрес обоих режимов API — `backend.completions_url` (прежнего
        # плоского `agent_url` в схеме нет).
        "backend.completions_url": URL,
        "backend.stream": False,
        "backend.api_mode": mode,
        "backend.chat_completions_max_tokens": 256,
        "backend.chat_completions_min_response_tokens": 64,
        "backend.request_timeout_sec": 600,
        "backend.use_ssl": False,
        "backend.use_retry": False,
        "agent.x_cl_id": "test-cl-id",
    }


def _ctx(tmp_path, cfg: dict, *, with_router: bool = True) -> SimpleNamespace:
    """Легаси-роутер (пустой реестр): модель запроса и «глаза» — одна модель."""
    loaded = make_settings(cfg)
    return fake_ctx(
        loaded=loaded,
        on_stream_delta=None,
        logger=logging.getLogger("test_outbound_images"),
        auth_connector=None,
        is_tui=False,
        model_router=ModelRouter(loaded, level_provider=lambda: "default")
        if with_router
        else None,
        image_store=ImageStore(str(tmp_path)),
    )


def _state(
    messages: list[dict], tools: list[dict] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        state_id="img-state",
        x_target=None,
        messages=messages,
        tools=list(tools or []),
        default_model=MODEL,
        usage=None,
        previous_response_id=None,
        call_metadata={},
    )


def _user_with_image(
    store: ImageStore, mid: str = "m1", n: int = 1, data: bytes = PNG
):
    att = store.stage(
        NormalizedImage(
            data=data,
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
    return att, {
        "role": "user",
        "id": mid,
        "content": f"[Изображение #{n}] что тут?",
        "attachments": [att.to_dict()],
    }


def _note(template: str, att) -> str:
    return template.format(label=att.label, wh=wh_text(att), path=att.path)


def _image_part(data: bytes = PNG) -> dict:
    return {
        "type": "image_url",
        "image_url": {
            "url": "data:image/png;base64,"
            + base64.b64encode(data).decode("ascii")
        },
    }


def _user_wire(outbound: list[dict]) -> list[dict]:
    return [m for m in outbound if m.get("role") == "user"]


def _tool_names(payload: dict) -> list[str]:
    return [tool["function"]["name"] for tool in payload["tools"]]


def test_live_user_image_reaches_the_wire_once(tmp_path, capture_outbound):
    """Батч повторяет хвост state по id — на проводе одно сообщение с картинкой."""
    ctx = _ctx(tmp_path, _cfg())
    _att, message = _user_with_image(ctx.image_store)
    state = _state([message])
    state.vision_routes = warm_routes(ctx)
    state.vision_probe_turn = "test-root"

    run_coro(pr._arequest_backend_canonical([message], state, ctx))

    assert _user_wire(capture_outbound["messages"]) == [
        {
            "role": "user",
            "content": [
                _image_part(),
                {"type": "text", "text": message["content"]},
            ],
        }
    ]


def test_state_history_and_new_batch_are_projected_together(
    tmp_path, capture_outbound
):
    ctx = _ctx(tmp_path, _cfg())
    _a1, old = _user_with_image(ctx.image_store, "m1", 1, b"first image bytes")
    _a2, new = _user_with_image(
        ctx.image_store, "m2", 2, b"second image bytes"
    )
    state = _state(
        [
            old,
            {
                "type": "message",
                "role": "assistant",
                "id": "a1",
                "content": "вижу",
            },
        ]
    )
    state.vision_routes = warm_routes(ctx)
    state.vision_probe_turn = "test-root"

    run_coro(pr._arequest_backend_canonical([new], state, ctx))

    assert _user_wire(capture_outbound["messages"]) == [
        {
            "role": "user",
            "content": [
                _image_part(b"first image bytes"),
                {"type": "text", "text": old["content"]},
            ],
        },
        {
            "role": "user",
            "content": [
                _image_part(b"second image bytes"),
                {"type": "text", "text": new["content"]},
            ],
        },
    ]


def test_images_section_of_the_config_reaches_the_wire(
    tmp_path, capture_outbound
):
    """Окно из конфига (`images.live_user_images: 1`) доходит до провода: пикселями —
    только самая свежая картинка, старая — запиской «вне контекста»."""
    cfg = {**_cfg(), "images": {"max_per_message": 1, "live_user_images": 1}}
    ctx = _ctx(tmp_path, cfg)
    a1, old = _user_with_image(ctx.image_store, "m1", 1, b"first image bytes")
    _a2, new = _user_with_image(
        ctx.image_store, "m2", 2, b"second image bytes"
    )
    state = _state(
        [
            old,
            {
                "type": "message",
                "role": "assistant",
                "id": "a1",
                "content": "вижу",
            },
        ],
        tools=[VIEW_TOOL],
    )
    state.vision_routes = warm_routes(ctx)
    state.vision_probe_turn = "test-root"

    run_coro(pr._arequest_backend_canonical([new], state, ctx))

    assert _user_wire(capture_outbound["messages"]) == [
        {
            "role": "user",
            "content": _note(STUB_OUT_OF_CONTEXT, a1) + "\n" + old["content"],
        },
        {
            "role": "user",
            "content": [
                _image_part(b"second image bytes"),
                {"type": "text", "text": new["content"]},
            ],
        },
    ]


def test_canon_is_untouched_and_audience_is_stored_for_the_tool(
    tmp_path, capture_outbound
):
    ctx = _ctx(tmp_path, _cfg())
    _att, message = _user_with_image(ctx.image_store)
    state = _state([message])
    state.vision_routes = warm_routes(ctx)
    state.vision_probe_turn = "test-root"
    before = copy.deepcopy(state.messages)

    run_coro(pr._arequest_backend_canonical([message], state, ctx))

    assert state.messages == before
    assert state.vision_audience.native.model == MODEL
    assert state.vision_live_ids == frozenset(
        {_att.id}
    )  # что ушло целиком — для тула


def test_blind_request_model_gets_notes_and_no_view_tool(
    tmp_path, capture_outbound
):
    ctx = _ctx(tmp_path, _cfg())
    att, message = _user_with_image(ctx.image_store)
    state = _state([message], tools=[READ_TOOL, VIEW_TOOL])
    state.vision_routes = warm_routes(ctx)
    state.vision_probe_turn = "test-root"
    mark_blind(route_key(ctx, url=URL, model=MODEL))

    run_coro(pr._arequest_backend_canonical([message], state, ctx))

    assert _user_wire(capture_outbound["messages"]) == [
        {
            "role": "user",
            "content": STUB_MODEL_REJECTS_NOBODY.format(
                label=att.label, wh=wh_text(att), alias=MODEL
            )
            + "\n"
            + message["content"],
        }
    ]
    assert _tool_names(capture_outbound["payload"]) == ["fs_read_file"]
    assert state.vision_audience.nobody is True
    assert state.vision_live_ids == frozenset()  # целиком не ушло ничего


def test_a_model_declared_text_only_gets_notes_without_a_rejection(
    tmp_path, capture_outbound
):
    """`backend.vision: false` — прокси молча выбрасывает картинку, отказа не
    будет: картинка не уходит с первого же запроса, без пометки процесса."""
    cfg = _cfg()
    cfg["backend.vision"] = False
    ctx = _ctx(tmp_path, cfg)
    att, message = _user_with_image(ctx.image_store)
    state = _state([message], tools=[READ_TOOL, VIEW_TOOL])
    state.vision_routes = warm_routes(ctx)
    state.vision_probe_turn = "test-root"

    run_coro(pr._arequest_backend_canonical([message], state, ctx))

    assert _user_wire(capture_outbound["messages"]) == [
        {
            "role": "user",
            "content": STUB_MODEL_REJECTS_NOBODY.format(
                label=att.label, wh=wh_text(att), alias=MODEL
            )
            + "\n"
            + message["content"],
        }
    ]
    assert _tool_names(capture_outbound["payload"]) == ["fs_read_file"]
    assert unsupported(route_key(ctx, url=URL, model=MODEL)) is False


def test_view_tool_stays_when_the_request_model_sees(
    tmp_path, capture_outbound
):
    ctx = _ctx(tmp_path, _cfg())
    state = _state(
        [{"role": "user", "id": "m1", "content": "привет"}],
        tools=[READ_TOOL, VIEW_TOOL],
    )
    state.vision_routes = warm_routes(ctx)
    state.vision_probe_turn = "test-root"

    run_coro(pr._arequest_backend_canonical(state.messages, state, ctx))

    assert _tool_names(capture_outbound["payload"]) == [
        "fs_read_file",
        FS_VIEW_IMAGE_NAME,
    ]


def test_context_without_router_means_nobody_sees(tmp_path, capture_outbound):
    ctx = _ctx(tmp_path, _cfg(), with_router=False)
    att, message = _user_with_image(ctx.image_store)
    state = _state([message])
    state.vision_routes = warm_routes(ctx)
    state.vision_probe_turn = "test-root"

    run_coro(pr._arequest_backend_canonical([message], state, ctx))

    assert _user_wire(capture_outbound["messages"]) == [
        {
            "role": "user",
            "content": _note(STUB_NOBODY, att) + "\n" + message["content"],
        }
    ]


def test_responses_mode_sends_notes_without_attachments(
    tmp_path, capture_responses
):
    """В responses нативного показа нет (спека §20.5): «глаза» есть — записка
    «модель не видит», ключа attachments в input нет, тул просмотра в наборе."""
    ctx = _ctx(tmp_path, _cfg("responses"))
    att, message = _user_with_image(ctx.image_store)
    state = _state([], tools=[VIEW_TOOL])
    state.vision_routes = warm_routes(ctx)
    state.vision_probe_turn = "test-root"

    run_coro(pr._arequest_backend_canonical([message], state, ctx))

    call_data = capture_responses["call_data"]
    assert call_data["input"] == [
        {
            "role": "user",
            "id": "m1",
            "content": _note(STUB_MODEL_BLIND, att)
            + "\n"
            + message["content"],
        }
    ]
    assert [tool["name"] for tool in call_data["tools"]] == [
        FS_VIEW_IMAGE_NAME
    ]


def test_responses_mode_hides_the_only_tool_when_nobody_sees(
    tmp_path, capture_responses
):
    """Явный пустой набор тулов не подменяется набором state."""
    ctx = _ctx(tmp_path, _cfg("responses"))
    state = _state([], tools=[VIEW_TOOL])
    state.vision_routes = warm_routes(ctx)
    state.vision_probe_turn = "test-root"
    mark_blind(route_key(ctx, url=URL, model=MODEL))

    run_coro(
        pr._arequest_backend_canonical(
            [{"role": "user", "id": "m1", "content": "привет"}], state, ctx
        )
    )

    assert capture_responses["call_data"]["tools"] == []
