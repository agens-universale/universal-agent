"""Native operation redelivery and error transport across the activity boundary."""

import asyncio
from types import SimpleNamespace

import pytest
from gc_core.execution.contracts import (
    ExecutionFailure,
    GuardPreconditionChanged,
    InfrastructureFailure,
    ToolOutcomeUnknown,
)
from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_ua_temporalio.native_activities import NativeActivities
from gc_ua_temporalio.native_contracts import NativeCall
from gc_ua_temporalio.public_config import public_config_snapshot
from gc_ua_temporalio.storage.technical import TechnicalStore


def activities(tmp_path):
    ctx = SimpleNamespace(workspace=SimpleNamespace(root=str(tmp_path)))

    async def provider(_call):
        return ctx

    receipts = TechnicalStore(str(tmp_path / "receipts.sqlite"))
    native = NativeActivities(
        context_provider=provider,
        stores={"files": DurableFileThreadStore(str(tmp_path / "threads"))},
        receipts=receipts,
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


async def test_domain_error_crosses_the_boundary_as_the_same_type(tmp_path):
    from gc_ua_temporalio.native_operations import restore_domain_error
    from temporalio.exceptions import ApplicationError

    native, _ = activities(tmp_path)

    async def empty_path(_call, _ctx):
        raise ValueError("path must not be empty")

    request = NativeCall("test", "v1", "owner", "probe")
    with pytest.raises(ApplicationError) as caught:
        await native._recorded(request, str, empty_path)
    assert caught.value.type == "DomainError"
    assert caught.value.non_retryable
    restored = restore_domain_error(caught.value)
    assert type(restored) is ValueError
    assert str(restored) == "path must not be empty"


@pytest.mark.parametrize(
    ("effect", "expected"),
    [(True, ToolOutcomeUnknown), (False, InfrastructureFailure)],
)
def test_worker_loss_is_tool_feedback_only_for_effects(effect, expected):
    from gc_ua_temporalio.native_operations import NativeOperations
    from temporalio.exceptions import ActivityError, RetryState, TimeoutError

    error = ActivityError(
        "timed out",
        scheduled_event_id=1,
        started_event_id=2,
        identity="worker",
        activity_type="agent.tool.execute",
        activity_id="tool-1",
        retry_state=RetryState.MAXIMUM_ATTEMPTS_REACHED,
    )
    error.__cause__ = TimeoutError(
        "heartbeat", type=None, last_heartbeat_details=[]
    )
    assert isinstance(
        NativeOperations._activity_error(
            error, "agent.tool.execute", effect=effect
        ),
        expected,
    )


async def test_precondition_change_is_recoverable_tool_feedback(tmp_path):
    from gc_ua_temporalio.native_operations import restore_domain_error

    native, _ = activities(tmp_path)

    async def changed(_call, _ctx):
        raise GuardPreconditionChanged("file changed")

    with pytest.raises(Exception) as caught:
        await native._recorded(
            NativeCall("test", "v1", "owner", "guarded"), str, changed
        )
    restored = restore_domain_error(caught.value)
    assert isinstance(restored, GuardPreconditionChanged)
    assert not isinstance(restored, ExecutionFailure)
