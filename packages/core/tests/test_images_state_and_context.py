"""Поля картинок в State и хранилище картинок в RunContext (спека §9.1, контракт задачи 18)."""

import json
from pathlib import Path

from gc_core import run_context as rc
from gc_core.attachments.store import ImageStore
from gc_core.model_router import ResolvedModel
from gc_core.persistence.state_serializer import (
    StateSerializer,
    load_state_from_snapshot,
)
from gc_core.security.media_roots import media_roots
from gc_core.states import State
from gc_core.vision.audience import Audience
from gc_core.vision.rejects import ImageRequestLimit
from gc_testkit import as_coro
from gc_testkit.memory_thread_store import MemoryThreadStore
from gc_testkit.settings import make_settings


def _state() -> State:
    return State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=2,
        system_prompt_id="v1",
        use_server_subagents=False,
    )


def _no_tools(monkeypatch):
    monkeypatch.setattr(
        rc,
        "abuild_tools_data",
        as_coro(lambda cfg, on_progress=None, **_kw: ({}, [], [], {})),
    )


def test_new_state_has_no_pending_images_and_no_audience():
    state = _state()
    assert state.pending_tool_attachments == []
    assert state.vision_audience is None
    assert state.vision_live_ids == frozenset()
    assert state.vision_request_limit is None


def test_serializer_keeps_pending_images_and_drops_the_audience():
    state = _state()
    state.pending_tool_attachments = [
        {"id": "0000000000000001", "origin": "tool"}
    ]
    state.vision_audience = Audience(
        native=ResolvedModel(
            alias="a",
            model="m",
            url="http://x",
            authorization=None,
            backend_overrides={},
            tariff=None,
            source="test",
        ),
        eyes=None,
        tool_visible=False,
    )
    state.vision_live_ids = frozenset({"0000000000000001"})
    state.vision_request_limit = ImageRequestLimit(
        ("http://backend", "m", 1, 0, frozenset({"image-id"})), 0
    )
    payload = StateSerializer.serialize(state)
    assert "vision_request_limit" not in payload
    assert "vision_audience" not in payload
    assert "vision_live_ids" not in payload
    restored = StateSerializer.deserialize(json.loads(json.dumps(payload)))
    assert restored.pending_tool_attachments == [
        {"id": "0000000000000001", "origin": "tool"}
    ]
    assert restored.vision_audience is None
    assert restored.vision_live_ids == frozenset()
    assert restored.vision_request_limit is None


def test_snapshot_older_than_the_fields_loads_with_defaults():
    payload = StateSerializer.serialize(_state())
    payload.pop("pending_tool_attachments")
    payload.pop("vision_request_limit", None)
    restored = load_state_from_snapshot(json.loads(json.dumps(payload)))
    assert restored.pending_tool_attachments == []
    assert restored.vision_audience is None
    assert restored.vision_live_ids == frozenset()
    assert restored.vision_request_limit is None


async def test_context_image_store_root_is_the_media_root_of_the_guards(
    tmp_path, monkeypatch
):
    _no_tools(monkeypatch)
    cfg = make_settings(
        {
            "agent.model": "M",
            "paths.threads_data_path": str(tmp_path),
            "logging.save_logs": False,
        }
    )
    ctx = await rc.acreate_run_context(cfg)
    assert (
        ctx.image_store.root == ImageStore.for_threads_dir(str(tmp_path)).root
    )
    assert Path(ctx.image_store.root) == media_roots(cfg)[0]


async def test_context_without_a_threads_dir_keeps_images_in_temp(monkeypatch):
    _no_tools(monkeypatch)
    ctx = await rc.acreate_run_context(
        make_settings({"agent.model": "M", "logging.save_logs": False})
    )
    assert ctx.image_store.root == ImageStore.for_threads_dir("").root


async def test_embedder_thread_store_does_not_move_images_off_the_guarded_root(
    tmp_path, monkeypatch
):
    """Своё хранилище тредов (у базы каталога нет) картинки не уводит: корень тот же,
    что охрана считает ``media`` (часть B, ``media_roots``)."""
    _no_tools(monkeypatch)
    cfg = make_settings(
        {
            "agent.model": "M",
            "paths.threads_data_path": str(tmp_path),
            "logging.save_logs": False,
        }
    )
    ctx = await rc.acreate_run_context(cfg, thread_store=MemoryThreadStore())
    assert Path(ctx.image_store.root) == media_roots(cfg)[0]
