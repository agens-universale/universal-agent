"""Картинки треда в хранилище тредов без файлов (база, S3, память SDK).

Вложения и снимки ложатся вложениями треда v2 (``put_blob``), в сообщении —
ссылка ``thread-file://…``; запрос к модели и ``fs_view_image`` читают байты
через ``get_blob``. На диске процесса — ничего: тред на другом инстансе
видит те же картинки.
"""

import logging
from types import SimpleNamespace

import httpx

from gc_core import processing_responses as pr
from gc_core.attachments.model import ORIGIN_USER, user_label
from gc_core.attachments.normalize import normalize_image
from gc_core.attachments.store import (
    ImageStore,
    StoreImageStore,
    image_store_for,
)
from gc_core.model_router import ModelRouter
from gc_core.thread_files import blob_ref_of, is_thread_file_uri
from gc_core.threads.file_store import FileThreadStore
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore
from gc_testkit.settings import fake_ctx, make_settings
from transport_fakes import install_transport
from vision_fakes import warm_routes

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf"
    b"\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_store_without_files_keeps_the_images(tmp_path):
    settings = make_settings({"paths.threads_data_path": str(tmp_path)})
    assert isinstance(
        image_store_for(settings, MemoryThreadStore()), StoreImageStore
    )
    files = image_store_for(settings, FileThreadStore(str(tmp_path)))
    assert isinstance(files, ImageStore)


async def test_snapshot_is_a_blob_of_the_thread(tmp_path):
    store = MemoryThreadStore()
    images = StoreImageStore(store)
    image = normalize_image(PNG)
    att = await images.asnapshot(
        image, "sys", "t1", origin="tool", label="x", source="s"
    )
    assert is_thread_file_uri(att.path)
    assert await store.get_blob(blob_ref_of(att.path)) == image.data
    assert await images.aread_for_send(att) == image.data
    # Байты — только в хранилище: другое хранилище по той же ссылке пусто.
    assert (
        await StoreImageStore(MemoryThreadStore()).aread_for_send(att) is None
    )


async def test_attachment_staged_on_disk_commits_into_the_store(tmp_path):
    """Путь SDK — вложение из кармана ``ImageStore`` на диске — доезжает в
    тред хранилища, а не теряется."""
    store = MemoryThreadStore()
    staged = ImageStore(str(tmp_path / "images")).stage(
        normalize_image(PNG), origin="user", label="x", source="s"
    )
    committed = await StoreImageStore(store).acommit(staged, "sdk", "t1")
    assert is_thread_file_uri(committed.path)
    assert (
        await store.get_blob(blob_ref_of(committed.path))
        == normalize_image(PNG).data
    )


async def test_live_image_of_the_store_reaches_the_model(monkeypatch):
    spy = install_transport(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            json={
                "id": "r1",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        ),
    )
    loaded = make_settings(
        {
            "agent.model": "seeing-model",
            "backend.completions_url": "http://llm.test/v1/chat/completions",
            "backend.api_mode": "chat_completions",
            "backend.chat_completions_max_tokens": 256,
            "backend.chat_completions_min_response_tokens": 64,
            "backend.request_timeout_sec": 30,
            "backend.use_ssl": False,
            "backend.use_retry": False,
            "agent.x_cl_id": "test-cl-id",
        }
    )
    images = StoreImageStore(MemoryThreadStore())
    att = await images.asnapshot(
        normalize_image(PNG),
        "sys",
        "t1",
        origin=ORIGIN_USER,
        label=user_label(1),
        source="clipboard",
    )
    ctx = fake_ctx(
        loaded=loaded,
        on_stream_delta=None,
        logger=logging.getLogger("test_thread_images_in_store"),
        auth_connector=None,
        is_tui=False,
        model_router=ModelRouter(loaded, choice_provider=lambda: "default"),
        image_store=images,
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
        state_id="store-state",
        x_target=None,
        messages=[message],
        tools=[],
        default_model="seeing-model",
        usage=None,
    )
    await pr._arequest_backend_canonical([message], state, ctx)
    assert b"image_url" in spy.requests[0].content
