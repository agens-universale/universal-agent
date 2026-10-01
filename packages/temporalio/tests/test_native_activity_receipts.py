"""Native operation redelivery and independent failure fences."""

import asyncio
from types import SimpleNamespace

import pytest
from gc_core.execution.contracts import (
    GuardPreconditionChanged,
    OutcomeUnknown,
)
from gc_core.thread_store_file import FileThreadStore
from gc_ua_temporalio.native_activities import NativeActivities
from gc_ua_temporalio.native_contracts import NativeCall
from gc_ua_temporalio.public_config import public_config_snapshot
from gc_ua_temporalio.storage.effects import EffectLedger
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore


def activities(tmp_path):
    ctx = SimpleNamespace(workspace=SimpleNamespace(root=str(tmp_path)))

    async def provider(_call):
        return ctx

    receipts = SQLiteThreadStore(str(tmp_path / "receipts.sqlite"))
    native = NativeActivities(
        context_provider=provider,
        stores={"files": FileThreadStore(str(tmp_path / "threads"))},
        receipts=receipts,
        effect_ledger=EffectLedger(receipts),
        public_config=public_config_snapshot,
    )
    return native, ctx


async def test_concurrent_redelivery_has_one_handler_and_bounded_lock_cache(
    tmp_path,
):
    native, _ = activities(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(_call, _ctx):
        calls.append(1)
        entered.set()
        await release.wait()
        return "recorded"

    request = NativeCall("test", "v1", "owner", "operation")
    first = asyncio.create_task(native._recorded(request, str, handler))
    await entered.wait()
    second = asyncio.create_task(native._recorded(request, str, handler))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(first, second) == ["recorded", "recorded"]
    assert calls == [1] and not native._operation_locks
    assert await native._recorded(request, str, handler) == "recorded"
    assert calls == [1] and not native._operation_locks


async def test_unknown_model_does_not_block_another_workspace_operation(
    tmp_path,
):
    native, ctx = activities(tmp_path)
    model = NativeCall("test", "v1", "owner", "model")

    async def lose_response():
        raise OSError("connection lost after request")

    with pytest.raises(OSError):
        await native._effect(
            model, ctx, lose_response, fence="model:owner:model"
        )
    with pytest.raises(OutcomeUnknown):
        await native._effect(
            model, ctx, lose_response, fence="model:owner:model"
        )

    async def other_effect():
        return "other completed"

    other = NativeCall("test", "v1", "other-owner", "tool")
    assert await native._effect(other, ctx, other_effect) == "other completed"


async def test_rejected_guard_is_replayed_as_rejected_without_mutation(
    tmp_path,
):
    native, ctx = activities(tmp_path)
    request = NativeCall("test", "v1", "owner", "guarded")
    calls = []

    async def rejected():
        calls.append(1)
        raise GuardPreconditionChanged("file changed")

    for _ in range(2):
        with pytest.raises(GuardPreconditionChanged):
            await native._effect(request, ctx, rejected)
    assert calls == [1]
    assert native.effects.get("owner", "guarded")["status"] == "REJECTED"
