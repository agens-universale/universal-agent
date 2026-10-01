"""Production provider uses actual core preparation and explicit deployment scope."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from gc_core.config_modules import AppConfig
from gc_core.execution.resource_contracts import ResourceOwnerLost
from gc_core.execution.resource_host import WorkerResourceHost
from gc_core.thread_store_file import FileThreadStore
from gc_ua_temporalio.native_contracts import NativeCall
from gc_ua_temporalio.native_provider import (
    NativeContextProvider,
    NativeProfile,
)
from gc_ua_temporalio.storage.effects import EffectLedger
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore


def profile(tmp_path, **overrides):
    config = AppConfig.model_validate(
        {
            "agent": {"use_local_subagents": False},
            "logging": {"save_logs": False},
        }
    )
    return NativeProfile(
        revision="v1", workspace=str(tmp_path), core=config, **overrides
    )


def make_provider(tmp_path, profiles=None, store=None):
    receipts = SQLiteThreadStore(str(tmp_path / "receipts.sqlite"))
    store = store or FileThreadStore(str(tmp_path / "threads"))
    return NativeContextProvider(
        profiles=profiles or {"test": profile(tmp_path)},
        stores={"files": store},
        receipts=receipts,
        resource_host=WorkerResourceHost(
            "test", ledger=EffectLedger(receipts)
        ),
    ), store


def call(**overrides):
    return replace(
        NativeCall("test", "v1", "owner", "prepare", {"store_id": "files"}),
        **overrides,
    )


@pytest.mark.parametrize("backend", ["file", "sqlite"])
async def test_actual_context_store_and_stateless_restart(
    tmp_path, monkeypatch, backend
):
    monkeypatch.chdir(tmp_path)
    store = (
        FileThreadStore(str(tmp_path / "threads"))
        if backend == "file"
        else SQLiteThreadStore(str(tmp_path / "threads.sqlite"))
    )
    provider, _ = make_provider(tmp_path, store=store)
    before = Path.cwd()
    try:
        ctx, same = await asyncio.gather(provider(call()), provider(call()))
        assert ctx is same
        assert ctx.workspace.root == str(tmp_path)
        assert ctx.thread_store is store
        assert ctx.tools_data["fs_read_file"][
            "tool_fabric"
        ].__module__.startswith("gc_core.")
        assert await provider.resource_owner(call()) is None
        assert Path.cwd() == before
        await provider.close(call())
        assert await provider(call(operation_id="accounting")) is ctx
        restarted, _ = make_provider(tmp_path, store=store)
        try:
            fresh = await restarted(call(operation_id="retry"))
            assert (
                fresh is not ctx and fresh.workspace.root == ctx.workspace.root
            )
        finally:
            await restarted.aclose()
    finally:
        await provider.aclose()


async def test_background_owner_is_lazy_and_cannot_be_recreated(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    provider, _ = make_provider(tmp_path)
    restarted = None
    try:
        ctx = await provider(call())
        assert await provider.resource_owner(call()) is None
        ref = await provider.ensure_resource_owner(call())
        assert await provider.resource_owner(call()) == ref
        assert await provider.ensure_resource_owner(call()) == ref
        assert await provider(call()) is ctx
        restarted, _ = make_provider(tmp_path)
        assert await restarted(call()) is not ctx  # stateless work survives
        with pytest.raises(ResourceOwnerLost):
            await restarted.ensure_resource_owner(call())
        await provider.close(call())
        await provider.close(call())
        assert await provider(call()) is ctx
        with pytest.raises(ResourceOwnerLost):
            await provider.ensure_resource_owner(call())
    finally:
        await provider.aclose()
        if restarted:
            await restarted.aclose()


async def test_revision_store_and_owner_bindings_are_pinned(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    provider, _ = make_provider(tmp_path)
    try:
        with pytest.raises(ValueError, match="revision"):
            await provider(call(revision="v2"))
        with pytest.raises(ValueError, match="store"):
            await provider(call(data={"store_id": "other"}))
        ctx = await provider(call())
        changed = profile(tmp_path)
        changed.core.agent.model_steering = "new behavior"
        with pytest.raises(Exception, match="different content"):
            make_provider(tmp_path, {"test": changed})
        assert await provider(call()) is ctx
    finally:
        await provider.aclose()


def test_wrong_workspace_rejected_without_chdir(tmp_path):
    before = Path.cwd()
    with pytest.raises(ValueError, match="start worker in workspace"):
        make_provider(tmp_path)
    assert Path.cwd() == before


async def test_actual_mcp_discovery_occurs_inside_resource_owner(
    tmp_path, monkeypatch
):
    import sys

    import gc_ua_temporalio.native_provider as module

    monkeypatch.chdir(tmp_path)
    configured = profile(tmp_path)
    configured.core.mcp = AppConfig.model_validate(
        {
            "logging": {"save_logs": False},
            "mcp": {
                "servers": {
                    "counter": {
                        "transport": "stdio",
                        "persistent_session": True,
                        "command": sys.executable,
                        "args": [
                            str(
                                Path(__file__).with_name(
                                    "mcp_fixture_server.py"
                                )
                            ),
                            "stdio",
                            "0",
                        ],
                    }
                }
            },
        }
    ).mcp
    events = []
    actual = module.acreate_run_context

    async def create(*args, **kwargs):
        events.append(asyncio.current_task())
        return await actual(*args, **kwargs)

    monkeypatch.setattr(module, "acreate_run_context", create)
    provider, _ = make_provider(tmp_path, {"test": configured})
    try:
        ctx = await provider(call())
        ref = await provider.resource_owner(call())
        assert ref is not None
        assert events == [provider.resource_host._owners["owner"]._task]
        assert events[0] is not asyncio.current_task()
        assert any(name.endswith("increment") for name in ctx.tools_data)
        await provider.close(call())
        assert await provider(call()) is ctx
    finally:
        await provider.aclose()


@pytest.mark.parametrize("live", [False, True])
async def test_child_uses_real_runner_and_parent_resource_owner(
    tmp_path, monkeypatch, live
):
    monkeypatch.chdir(tmp_path)
    configured = profile(tmp_path)
    configured.core.agent.use_local_subagents = True
    configured.core.backend.api_mode = "chat_completions"
    provider, _ = make_provider(tmp_path, {"test": configured})
    try:
        parent = await provider(call())
        ref = await provider.ensure_resource_owner(call()) if live else None
        child_call = call(
            owner_id="child",
            resource_owner=ref,
            child_context={
                "parent_owner_id": "owner",
                "parent_context": None,
                "tool_name": "subagent_worker",
                "frozen_level": "default",
                "force_legacy": False,
                "circuit_breaker": {},
            },
        )
        child = await provider(child_call)
        assert child is not parent
        assert child.os_backend is parent.os_backend
        assert child.mcp_session_owner == parent.mcp_session_owner
        assert child.run_id == "child"
        assert child._native_parent_context.workspace is parent.workspace
        assert await provider.resource_owner(child_call) == ref
        await provider.close(child_call)
        if ref is not None:
            assert await provider.ensure_resource_owner(call()) == ref
        await provider.release(call())
        assert (
            not provider._contexts
            and not provider._refs
            and not provider._parents
        )
        assert not provider.resource_host._owners
    finally:
        await provider.aclose()


async def test_poststeps_release_drops_context_logger_and_owner_cache(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    provider, _ = make_provider(tmp_path)
    released = []
    monkeypatch.setattr(
        "gc_core.utils.logging_module.release_run_logger", released.append
    )
    ctx = await provider(call())
    await provider.ensure_resource_owner(call())
    await provider.close(call())
    assert await provider(call(operation_id="accounting")) is ctx
    await provider.release(call(operation_id="release"))
    await provider.release(call(operation_id="release"))
    assert released == [ctx.logger]
    assert (
        not provider._contexts and not provider._refs and not provider._locks
    )
    await provider.aclose()


@pytest.mark.parametrize("operation", ["close", "release"])
async def test_cleanup_retry_after_handler_before_receipt_never_rebuilds_context(
    tmp_path, monkeypatch, operation
):
    from gc_ua_temporalio.native_activities import NativeActivities
    from gc_ua_temporalio.public_config import public_config_snapshot

    monkeypatch.chdir(tmp_path)
    provider, store = make_provider(tmp_path)
    await provider(call())
    await provider.ensure_resource_owner(call())

    def activities(context_provider):
        return NativeActivities(
            context_provider=context_provider,
            stores={"files": store},
            receipts=context_provider.receipts,
            effect_ledger=EffectLedger(context_provider.receipts),
            public_config=public_config_snapshot,
        )

    request = call(operation_id=operation)
    save = provider.receipts.save_operation_result

    def crash_before_receipt(owner, operation_id, digest, result):
        if operation_id == operation:
            raise RuntimeError("process lost after cleanup")
        return save(owner, operation_id, digest, result)

    monkeypatch.setattr(
        provider.receipts, "save_operation_result", crash_before_receipt
    )
    with pytest.raises(RuntimeError, match="process lost"):
        await getattr(activities(provider), operation)(request)
    monkeypatch.setattr(provider.receipts, "save_operation_result", save)
    restarted, _ = make_provider(tmp_path, store=store)

    async def forbidden(_call):
        raise AssertionError("Cleanup retry must not reconstruct a context")

    # If NativeActivities resolves this provider, its inherited __call__ would
    # otherwise rebuild a stateless context. Make the attempted resolution loud.
    class CleanupOnly:
        close = restarted.close
        release = restarted.release
        receipts = restarted.receipts
        __call__ = staticmethod(forbidden)

    try:
        restored = activities(CleanupOnly())
        assert await getattr(restored, operation)(request) is True
        assert await getattr(restored, operation)(request) is True
        assert not restarted._contexts
    finally:
        await provider.aclose()
        await restarted.aclose()
