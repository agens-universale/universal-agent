"""Восстановление после потери процесса, владение и терминальные статусы."""

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from gc_core.threads.errors import ThreadConflict
from gc_core.threads.models import SideChange
from gc_server.chat.responses.facade import ResponsesFacade
from gc_server.chat.responses.index_store import (
    ResponseIndex,
    ResponseRecord,
    thread_key,
)
from gc_server.config import ChatConfig, ChatRecoveryConfig
from gc_testkit.settings import make_settings


class Agent:
    def __init__(self, calls):
        self.calls = calls
        self.executor = SimpleNamespace(
            last_terminal_outcome=None, _ctx=SimpleNamespace()
        )
        self.last_answer = "recovered"

    async def stream(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        yield "answer", self.last_answer

    async def aclose(self):
        pass


def facade(tmp_path, calls, **options):
    async def factory(settings, **kwargs):
        del settings, kwargs
        return Agent(calls)

    return ResponsesFacade(
        settings=make_settings({"agent.model": "test"}),
        agent_factory=factory,
        index=ResponseIndex.for_threads_dir(str(tmp_path / "threads")),
        section=ChatConfig(
            launch_root=str(tmp_path), recovery=ChatRecoveryConfig(**options)
        ),
    )


async def checkpoint(f, tmp_path, **changes):
    record = ResponseRecord(
        "resp_00000000-0000-4000-8000-000000000001",
        "dialog",
        str(tmp_path),
        None,
        int(time.time()),
        "in_progress",
        {
            "id": "resp_00000000-0000-4000-8000-000000000001",
            "status": "in_progress",
            "model": "GC-UA",
            "metadata": {},
        },
        reservation="dead",
        request_text="создай отчёт",
    )
    record = replace(record, **changes)
    await f._index.threads.store.append_side(
        thread_key("dialog"),
        SideChange(
            op_id="old-admission",
            patch_frontend=(
                "chat",
                {"admission": {"token": "dead", "expires": time.time() - 1}},
            ),
        ),
    )
    await f._index.save(record)
    return record


async def settled(f):
    runs = list(f._live.values())
    for run in runs:
        await asyncio.wait_for(run.done.wait(), 2)


async def test_restarts_same_response_from_saved_input_and_only_one_owner(
    tmp_path,
):
    calls = []
    first = facade(tmp_path, calls)
    old = await checkpoint(first, tmp_path)
    a, b = facade(tmp_path, calls), facade(tmp_path, calls)
    await asyncio.gather(a.recovery.recover_once(), b.recovery.recover_once())
    await settled(a)
    await settled(b)
    saved = await a._index.load(old.response_id)
    assert saved.status == "completed"
    assert saved.recovery_attempts == 1
    assert saved.request_text == old.request_text
    assert len(calls) == 1
    assert old.request_text in calls[0][0]
    assert calls[0][1]["cancel_target"] == old.response_id
    await a.shutdown()
    await b.shutdown()


@pytest.mark.parametrize(
    "options,changes,code",
    [
        ({"enabled": False}, {}, "execution_interrupted"),
        ({}, {"request_text": None}, "execution_interrupted"),
        ({}, {"created_at": 1}, "execution_interrupted"),
        ({"max_attempts": 2}, {"recovery_attempts": 2}, "recovery_exhausted"),
    ],
)
async def test_unrecoverable_answers_become_terminal(
    tmp_path, options, changes, code
):
    calls = []
    f = facade(tmp_path, calls, **options)
    old = await checkpoint(f, tmp_path, **changes)
    await f.recovery.recover_once()
    result = await f.get(old.response_id)
    assert result["status"] == "failed"
    assert result["error"]["code"] == code
    assert calls == []
    await f.shutdown()


async def test_live_admission_is_not_stolen(tmp_path):
    calls = []
    f = facade(tmp_path, calls)
    old = await checkpoint(f, tmp_path)
    await f._index.threads.store.append_side(
        thread_key("dialog"),
        SideChange(
            op_id="renew",
            patch_frontend=(
                "chat",
                {"admission": {"token": "dead", "expires": time.time() + 120}},
            ),
        ),
    )
    assert (await f.get(old.response_id))["status"] == "in_progress"
    assert calls == []
    assert not f._busy


async def test_cancel_orphan_never_launches_agent(tmp_path):
    calls = []
    f = facade(tmp_path, calls)
    old = await checkpoint(f, tmp_path)
    assert (await f.cancel(old.response_id))["status"] == "in_progress"
    await f.recovery.recover_once()
    assert (await f.get(old.response_id))["status"] == "cancelled"
    assert calls == []


async def test_old_finalizer_cannot_overwrite_recovered_answer(tmp_path):
    f = facade(tmp_path, [])
    old = await checkpoint(f, tmp_path)
    await f.recovery.recover_once()
    await settled(f)
    with pytest.raises(ThreadConflict):
        await f._index.save(
            replace(old, status="failed", response={"status": "failed"})
        )
    assert (await f._index.load(old.response_id)).status == "completed"
    # Повторный обход не запускает уже завершённый ответ.
    await f.recovery.recover_once()
    assert not f._live
    await f.shutdown()


async def test_loop_recovers_without_polling_client(tmp_path):
    calls = []
    f = facade(tmp_path, calls, interval_seconds=0.01)
    old = await checkpoint(f, tmp_path)
    f.recovery.start()
    try:

        async def wait_completed():
            while (await f._index.load(old.response_id)).status != "completed":
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_completed(), 2)
        assert len(calls) == 1
    finally:
        await f.shutdown()


@pytest.mark.parametrize("completed", [False, True])
async def test_killed_process_is_resumed_by_fresh_facade(tmp_path, completed):
    """Настоящий процесс с идущим ходом убит; новая facade читает только диск."""
    import sys
    from pathlib import Path

    script = """
import asyncio, runpy, sys
from pathlib import Path
from gc_server.chat.responses import admission
ns = runpy.run_path(sys.argv[1])
root = Path(sys.argv[2])
completed = sys.argv[3] == 'completed'
admission.TTL = 0.5
admission.RENEW = 0.1
async def main():
    f = ns['facade'](root, [])
    class Blocking(ns['Agent']):
        async def stream(self, prompt, **kwargs):
            if completed:
                (root / 'action').write_text('executed once')
                yield 'answer', 'done'
            else:
                (root / 'started').write_text(kwargs['cancel_target'])
                await asyncio.Event().wait()
                yield 'answer', 'never'
    async def factory(settings, **kwargs):
        return Blocking([])
    f._agent_factory = factory
    if completed:
        async def hold_release(*args):
            await asyncio.Event().wait()
        f._release = hold_release
    run = await f.open({'model': 'GC-UA', 'input': 'survive process death'})
    if completed:
        async for chunk in run.iter_sse():
            if b'response.completed' in chunk:
                (root / 'started').write_text(run.response_id)
    await asyncio.Event().wait()
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(Path(__file__).resolve()),
        str(tmp_path),
        "completed" if completed else "running",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:

        async def started():
            while not (tmp_path / "started").exists():
                if process.returncode is not None:
                    raise AssertionError(
                        (await process.stderr.read()).decode()
                    )
                await asyncio.sleep(0.02)

        await asyncio.wait_for(started(), 10)
        response_id = (tmp_path / "started").read_text()
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
    await asyncio.sleep(0.6)
    calls = []
    restarted = facade(tmp_path, calls)
    await restarted.recovery.recover_once()
    await settled(restarted)
    result = await restarted.get(response_id)
    assert result["status"] == "completed"
    if completed:
        assert calls == []
        assert (tmp_path / "action").read_text() == "executed once"
    else:
        assert len(calls) == 1
        assert "survive process death" in calls[0][0]
    await restarted.shutdown()


async def test_superseded_orphan_does_not_replace_dialog_summary(tmp_path):
    calls = []
    f = facade(tmp_path, calls)
    old = await checkpoint(f, tmp_path)
    newer = replace(
        old,
        response_id="resp_00000000-0000-4000-8000-000000000002",
        previous_response_id=old.response_id,
        status="completed",
        response={"status": "completed"},
    )
    await f._index.save(newer)
    await f.recovery.recover_once()
    assert (await f._index.load(old.response_id)).status == "failed"
    thread = await f._index.threads.get(thread_key("dialog"))
    assert f._index.summary(thread)["latest_response_id"] == newer.response_id
    assert calls == []


async def test_get_is_one_read_and_never_recovers(tmp_path, monkeypatch):
    calls = []
    f = facade(tmp_path, calls)
    old = await checkpoint(f, tmp_path)
    load = f._index.load
    reads = []

    async def tracked(response_id):
        reads.append(response_id)
        return await load(response_id)

    monkeypatch.setattr(f._index, "load", tracked)
    assert (await f.get(old.response_id))["status"] == "in_progress"
    assert reads == [old.response_id]
    assert not calls and not f._busy and not f._admissions


async def test_recovery_retries_transient_launch_failure(
    tmp_path, monkeypatch
):
    calls = []
    f = facade(tmp_path, calls, max_attempts=3)
    old = await checkpoint(f, tmp_path)
    original = f._agent_factory
    attempts = 0

    async def create(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary outage")
        return await original(*args, **kwargs)

    monkeypatch.setattr(f, "_agent_factory", create)
    await f.recovery.recover_once()
    saved = await f._index.load(old.response_id)
    assert saved.status == "in_progress" and saved.recovery_attempts == 1
    await f.recovery.recover_once()
    await settled(f)
    assert (await f.get(old.response_id))["status"] == "completed"
    assert attempts == 2 and len(calls) == 1
    await f.shutdown()


async def test_failed_open_is_not_executed_by_recovery(tmp_path, monkeypatch):
    calls = []
    f = facade(tmp_path, calls)

    async def fail(*args, **kwargs):
        raise OSError("tool history unavailable")

    monkeypatch.setattr("gc_server.chat.responses.facade.ToolFlow.start", fail)
    with pytest.raises(OSError):
        await f.open({"model": "GC-UA", "input": "act"})
    records = [r for rows in f._index.legacy_dialogs().values() for r in rows]
    assert len(records) == 1 and records[0].status == "failed"
    await f.recovery.recover_once()
    assert calls == []
    assert not f._busy
    await f.shutdown()


async def test_terminal_event_follows_saved_status(tmp_path, monkeypatch):
    calls = []
    f = facade(tmp_path, calls)
    entered, release = asyncio.Event(), asyncio.Event()
    save = f._index.save

    async def held(record, **kwargs):
        if record.status == "completed":
            entered.set()
            await release.wait()
        await save(record, **kwargs)

    monkeypatch.setattr(f._index, "save", held)
    run = await f.open({"model": "GC-UA", "input": "act"})
    await asyncio.wait_for(entered.wait(), 2)
    seen = []
    while not run._queue.empty():
        seen.append(run._queue.get_nowait()["type"])
    assert "response.completed" not in seen
    assert (await f._index.load(run.response_id)).status == "in_progress"
    release.set()
    await asyncio.wait_for(run.done.wait(), 2)
    assert (await f._index.load(run.response_id)).status == "completed"
    assert any(
        [
            "response.completed" in chunk.decode()
            async for chunk in run.iter_sse()
        ]
    )
    await f.shutdown()


async def test_failed_final_save_retries_only_the_record(
    tmp_path, monkeypatch
):
    calls = []
    f = facade(tmp_path, calls)
    save = f._index.save
    unavailable = True

    async def broken(record, **kwargs):
        if record.status == "completed" and unavailable:
            raise OSError("temporary disk outage")
        await save(record, **kwargs)

    monkeypatch.setattr(f._index, "save", broken)
    run = await f.open({"model": "GC-UA", "input": "act"})
    await asyncio.wait_for(run.done.wait(), 2)
    result = await run.result()
    assert result["status"] == "failed"
    assert result["error"]["code"] == "response_persistence_failed"
    assert run.thread_id in f._busy and run.thread_id in f._admissions
    await f.recovery.recover_once()
    assert len(calls) == 1
    unavailable = False
    await f.recovery.recover_once()
    assert (await f._index.load(run.response_id)).status == "completed"
    assert len(calls) == 1 and not f._busy and not f._live
    await f.shutdown()


async def test_completed_archive_is_read_once_without_loading_every_thread(
    tmp_path, monkeypatch
):
    f = facade(tmp_path, [])
    import threading
    from uuid import uuid4

    old = await checkpoint(f, tmp_path, status="completed")
    expected = {old.response_id}
    for i in range(299):
        record = replace(
            old, response_id="resp_" + str(uuid4()), thread_id=f"done-{i}"
        )
        f._index._write_legacy(record)
        expected.add(record.response_id)
    loop_thread = threading.get_ident()
    read = f._index._legacy
    reads = []

    def counted(response_id):
        assert threading.get_ident() != loop_thread
        reads.append(response_id)
        return read(response_id)

    history_reads = []

    async def unexpected(*args, **kwargs):
        history_reads.append(args)
        return []

    monkeypatch.setattr(f._index, "_legacy", counted)
    monkeypatch.setattr(f._index, "thread_records", unexpected)
    await f.recovery.recover_once()
    assert len(reads) == 300 and set(reads) == expected
    assert history_reads == []
    await f.shutdown()


@pytest.mark.parametrize("permanent", [False, True])
async def test_recovery_launch_respects_attempt_limit_and_api_code(
    tmp_path, permanent
):
    from gc_server.chat.errors import ApiError

    f = facade(tmp_path, [], max_attempts=2)
    record = await checkpoint(f, tmp_path)
    attempts = []

    async def broken(settings, **kwargs):
        attempts.append(1)
        if permanent:
            raise ApiError(
                400, "invalid_request", "cwd is outside launch_root"
            )
        raise OSError("temporary setup failure")

    f._agent_factory = broken
    for _ in range(3):
        await f.recovery.recover_once()
    result = await f.get(record.response_id)
    assert result["status"] == "failed"
    assert result["error"]["code"] == (
        "invalid_request" if permanent else "server_error"
    )
    assert len(attempts) == (1 if permanent else 2)
    await f.shutdown()


async def test_failed_finalizer_yields_to_new_admission(tmp_path, monkeypatch):
    calls = []
    f = facade(tmp_path, calls)
    save = f._index.save
    stolen = False

    async def steal(record, **kwargs):
        nonlocal stolen
        if record.status == "completed" and not stolen:
            stolen = True
            await f._index.threads.store.append_side(
                thread_key(record.thread_id),
                SideChange(
                    op_id="other-owner",
                    patch_frontend=(
                        "chat",
                        {
                            "admission": {
                                "token": "new-owner",
                                "expires": time.time() + 120,
                            },
                        },
                    ),
                ),
            )
        await save(record, **kwargs)

    monkeypatch.setattr(f._index, "save", steal)
    run = await f.open({"model": "GC-UA", "input": "act"})
    await asyncio.wait_for(run.done.wait(), 2)
    assert (await run.result())["error"][
        "code"
    ] == "response_persistence_failed"
    await f.recovery.recover_once()
    assert not f._busy and not f._live and len(calls) == 1
    thread = await f._index.threads.get(thread_key(run.thread_id))
    assert thread.frontend["chat"]["admission"]["token"] == "new-owner"
    await f.shutdown()
