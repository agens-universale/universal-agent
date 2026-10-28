"""Картинка читается с диска один раз на запрос: проекция идёт по двум спискам
(``state.messages`` и батч), и картинки батча есть в обоих (ревью 2026-10-01)."""

import logging
from types import SimpleNamespace

import httpx

from gc_core import processing_responses as pr
from gc_core.attachments.model import ORIGIN_USER, user_label
from gc_core.attachments.normalize import NormalizedImage
from gc_core.attachments.store import ImageStore
from gc_core.model_router import ModelRouter
from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx, make_settings
from vision_fakes import warm_routes
from transport_fakes import install_transport

URL = "http://llm.test/v1/chat/completions"
OK = {
    "id": "r1",
    "choices": [
        {
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
}


def test_a_live_image_is_read_once_per_request(tmp_path, monkeypatch):
    spy = install_transport(
        monkeypatch, lambda _request: httpx.Response(200, json=OK)
    )
    cfg = {
        "agent.model": "seeing-model",
        "backend.completions_url": URL,
        "backend.api_mode": "chat_completions",
        "backend.chat_completions_max_tokens": 256,
        "backend.chat_completions_min_response_tokens": 64,
        "backend.request_timeout_sec": 30,
        "backend.use_ssl": False,
        "backend.use_retry": False,
        "agent.x_cl_id": "test-cl-id",
    }
    store = ImageStore(str(tmp_path))
    att = store.stage(
        NormalizedImage(
            data=b"\x89PNG read once",
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
    reads = []
    aread_for_send = store.aread_for_send

    async def counting_read(attachment):
        reads.append(attachment.id)
        return await aread_for_send(attachment)

    monkeypatch.setattr(store, "aread_for_send", counting_read)
    loaded = make_settings(cfg)
    ctx = fake_ctx(
        loaded=loaded,
        on_stream_delta=None,
        logger=logging.getLogger("test_image_request_reads"),
        auth_connector=None,
        is_tui=False,
        model_router=ModelRouter(loaded, level_provider=lambda: "default"),
        image_store=store,
        on_trace_note=lambda _note: None,
    )
    warm_routes(ctx)
    message = {
        "role": "user",
        "id": "m1",
        "content": "[Изображение #1] что тут?",
        "attachments": [att.to_dict()],
    }
    state = SimpleNamespace(
        vision_probe_turn="test-root",
        state_id="reads-state",
        x_target=None,
        messages=[message],
        tools=[],
        default_model="seeing-model",
        usage=None,
    )

    run_coro(pr._arequest_backend_canonical([message], state, ctx))

    assert b"image_url" in spy.requests[0].content
    assert reads == [att.id]
