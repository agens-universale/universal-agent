"""Поля картинок в State и хранилище картинок в RunContext (спека §9.1, контракт задачи 18)."""

from pathlib import Path

from gc_core import run_context as rc
from gc_core.attachments.store import ImageStore
from gc_core.security.media_roots import media_roots
from gc_core.states import State
from gc_testkit import as_coro
from gc_testkit.memory_thread_store_v2 import (
    MemoryThreadStore,
)
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


async def test_embedder_thread_store_keeps_images_in_the_store(
    tmp_path, monkeypatch
):
    """Своё хранилище тредов (у базы каталога нет) держит картинки у себя:
    на диске процесса ни каталога, ни файла (подробно —
    ``test_thread_images_in_store``)."""
    from gc_core.attachments.store import StoreImageStore

    _no_tools(monkeypatch)
    cfg = make_settings(
        {
            "agent.model": "M",
            "paths.threads_data_path": str(tmp_path / "threads"),
            "logging.save_logs": False,
        }
    )
    ctx = await rc.acreate_run_context(cfg, thread_store=MemoryThreadStore())
    assert isinstance(ctx.image_store, StoreImageStore)
    assert not (tmp_path / "threads").exists()
