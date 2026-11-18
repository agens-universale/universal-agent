"""Аренда треда и опрос входящих не переживают сбой и отмену хода.

Две фоновые задачи стартуют в начале хода. Ход, упавший до цикла или
отменённый посреди уборки, не должен оставлять их жить: на долгоживущем
цикле их некому убрать.
"""

import asyncio
from types import SimpleNamespace

import gc_core.agent_executor as ae
import pytest
from gc_core.run_context import acreate_run_context
from gc_core.utils.logging_module import release_run_logger
from gc_testkit.settings import make_settings

SETTINGS = {
    "logging.save_logs": False,
    "backend.api_mode": "chat_completions",
    "backend.completions_url": "http://test",
    "agent.model": "x",
    "agent.max_steps": 1,
    "agent.system_prompt_id": "v1",
}


async def _settle() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.fixture
async def env(tmp_path, monkeypatch):
    settings = {**SETTINGS, "paths.threads_data_path": str(tmp_path)}
    ctx = await acreate_run_context(
        make_settings(settings), auth_connector=None
    )
    ex = ae.AgentExecutor(ctx)
    started: list[asyncio.Task] = []
    box = SimpleNamespace(ex=ex, started=started, teardowns=0)

    async def _loop(*_args, **_kwargs):
        await asyncio.sleep(0.12)  # аренда успевает продлиться
        return {"finished": True}

    monkeypatch.setattr(ae, "arun_agent", _loop)
    monkeypatch.setattr(ae, "LEASE_RENEW_SECONDS", 0.05)

    real_keeper = ae._start_lease_keeper
    real_poller = ex._start_inbox_poller
    real_teardown = ex._ateardown_run

    def _keeper(*args, **kwargs):
        task = real_keeper(*args, **kwargs)
        if task is not None:
            started.append(task)
        return task

    def _poller(thread_session):
        task = real_poller(thread_session)
        if task is not None:
            started.append(task)
        return task

    async def _teardown(owner_reset):
        box.teardowns += 1
        return await real_teardown(owner_reset)

    monkeypatch.setattr(ae, "_start_lease_keeper", _keeper)
    monkeypatch.setattr(ex, "_start_inbox_poller", _poller)
    monkeypatch.setattr(ex, "_ateardown_run", _teardown)

    async def run():
        return await ex.arun(
            ["привет"], thread_id="t", start_new_thread=True, system="cli"
        )

    box.run = run
    yield box
    release_run_logger(ctx.logger)


async def test_plain_turn_stops_lease_and_poller(env):
    await env.run()
    await _settle()
    assert env.started and all(t.done() for t in env.started)
    assert env.teardowns == 1


async def test_failure_before_the_loop_keeps_the_error_and_cleans_up(
    env, monkeypatch
):
    def _boom():
        raise RuntimeError("стоп не взвёлся")

    monkeypatch.setattr(env.ex, "_user_stop_scope", _boom)
    with pytest.raises(RuntimeError, match="стоп не взвёлся"):
        await env.run()
    await _settle()
    assert env.started and all(t.done() for t in env.started)
    assert env.teardowns == 1


async def test_cancel_during_cleanup_still_tears_the_turn_down(
    env, monkeypatch
):
    real = ae._stop_lease_keeper
    hits = []

    async def _cancelled_once(task):
        hits.append(task)
        if len(hits) == 1:
            asyncio.current_task().cancel()
        await real(task)

    monkeypatch.setattr(ae, "_stop_lease_keeper", _cancelled_once)
    with pytest.raises(asyncio.CancelledError):
        await env.run()
    await _settle()
    assert env.started and all(t.done() for t in env.started)
    assert env.teardowns == 1
