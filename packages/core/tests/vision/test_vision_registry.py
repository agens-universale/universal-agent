import asyncio
import gc
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from gc_core.vision.probe import ProbeResult, ProbeStatus
from gc_core.vision.registry import VisionProbeRegistry
from gc_core.vision.route import VisionRouteKey

KEY = VisionRouteKey("https://example.test/chat", "m", "test-key")
OK = ProbeResult(ProbeStatus.CONFIRMED, "code_read")


@pytest.mark.parametrize(
    "result",
    [
        OK,
        ProbeResult(ProbeStatus.UNCONFIRMED, "code_mismatch"),
        ProbeResult(ProbeStatus.INSUFFICIENT_BUDGET, "output_limit"),
        ProbeResult(ProbeStatus.BLOCKED, "access_denied"),
    ],
)
async def test_results_cached_across_turns(result):
    registry, calls = VisionProbeRegistry(), []

    async def run():
        calls.append(1)
        return result

    for turn in ["a", "b"]:
        assert await registry.ensure(KEY, turn_key=turn, run=run) == result
    assert calls == [1]
    registry.mark_unsupported(KEY, reason="image_unsupported")
    assert registry.peek(KEY).status is ProbeStatus.UNSUPPORTED
    assert (
        registry.peek(VisionRouteKey(KEY.url, KEY.model, "other-access"))
        is None
    )


async def test_transient_needs_new_turn_and_elapsed_pause():
    now, calls = [0.0], []
    registry = VisionProbeRegistry(clock=lambda: now[0])

    async def run():
        calls.append(1)
        return ProbeResult(ProbeStatus.TRANSIENT, "network", 30)

    await registry.ensure(KEY, turn_key="a", run=run)
    await registry.ensure(KEY, turn_key="b", run=run)
    now[0] = 31
    await registry.ensure(KEY, turn_key="a", run=run)
    await registry.ensure(KEY, turn_key="b", run=run)
    assert len(calls) == 1
    await registry.ensure(KEY, turn_key="c", run=run)
    now[0] = 62
    await registry.ensure(KEY, turn_key="a", run=run)
    await registry.ensure(KEY, turn_key="b", run=run)
    assert len(calls) == 2
    await registry.ensure(KEY, turn_key="d", run=run)
    assert len(calls) == 3


async def test_waiter_cancellation_does_not_cancel_owner():
    registry = VisionProbeRegistry()
    started, release = asyncio.Event(), asyncio.Event()

    async def run():
        started.set()
        await release.wait()
        return OK

    owner = asyncio.create_task(registry.ensure(KEY, turn_key="a", run=run))
    await started.wait()
    waiter = asyncio.create_task(registry.ensure(KEY, turn_key="b", run=run))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    assert await owner == OK
    assert registry.peek(KEY) == OK


async def test_owner_cancellation_releases_waiters_without_negative_cache():
    registry = VisionProbeRegistry()
    started = asyncio.Event()

    async def run():
        started.set()
        await asyncio.Event().wait()

    owner = asyncio.create_task(registry.ensure(KEY, turn_key="a", run=run))
    await started.wait()
    waiter = asyncio.create_task(registry.ensure(KEY, turn_key="b", run=run))
    await asyncio.sleep(0)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert (await asyncio.wait_for(waiter, 2)).reason == "probe_cancelled"
    assert registry.peek(KEY) is None
    assert (
        await registry.ensure(
            KEY, turn_key="a", run=lambda: asyncio.sleep(0, result=OK)
        )
        == OK
    )


def test_two_loops_share_one_probe():
    registry, calls = VisionProbeRegistry(), []
    started, joined, release = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    async def run():
        calls.append(1)
        started.set()
        assert await asyncio.to_thread(release.wait, 2)
        return OK

    def owner():
        return asyncio.run(registry.ensure(KEY, turn_key="a", run=run))

    def waiter():
        assert started.wait(2)

        async def wait():
            task = asyncio.create_task(
                registry.ensure(KEY, turn_key="b", run=run)
            )
            await asyncio.sleep(0)
            joined.set()
            return await task

        return asyncio.run(wait())

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(owner), pool.submit(waiter)
        assert joined.wait(2)
        release.set()
        assert first.result(3) == second.result(3) == OK
    assert calls == [1]


def test_owner_loop_shutdown_releases_other_loop():
    registry = VisionProbeRegistry()
    started, joined, stop = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    async def run():
        started.set()
        await asyncio.Event().wait()

    def owner():
        async def drive():
            # Keep a strong reference until shutdown can cancel the task.
            # The loop itself only holds weak references to pending tasks.
            task = asyncio.create_task(
                registry.ensure(KEY, turn_key="a", run=run)
            )
            assert await asyncio.to_thread(stop.wait, 2)
            assert not task.done()

        asyncio.run(drive())  # shutdown cancels and drains the owner's task

    def waiter():
        assert started.wait(2)
        gc.collect()

        async def drive():
            task = asyncio.create_task(
                registry.ensure(KEY, turn_key="b", run=run)
            )
            await asyncio.sleep(0)
            joined.set()
            return await asyncio.wait_for(task, 3)

        return asyncio.run(drive())

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(owner), pool.submit(waiter)
        assert joined.wait(2)
        stop.set()
        first.result(3)
        assert second.result(3).reason == "probe_cancelled"
    assert registry.peek(KEY) is None
    assert (
        asyncio.run(
            registry.ensure(
                KEY, turn_key="c", run=lambda: asyncio.sleep(0, result=OK)
            )
        )
        == OK
    )


async def test_cancelled_retry_keeps_previous_failed_turn_suppressed():
    now, calls = [0.0], []
    registry = VisionProbeRegistry(clock=lambda: now[0])
    failure = ProbeResult(ProbeStatus.TRANSIENT, "network", 30)

    async def failed():
        calls.append(1)
        return failure

    await registry.ensure(KEY, turn_key="already-failed", run=failed)
    now[0] = 31
    started = asyncio.Event()

    async def cancelled():
        started.set()
        await asyncio.Event().wait()

    owner = asyncio.create_task(
        registry.ensure(KEY, turn_key="new-turn", run=cancelled)
    )
    await started.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert (
        await registry.ensure(KEY, turn_key="already-failed", run=failed)
        == failure
    )
    assert calls == [1]
