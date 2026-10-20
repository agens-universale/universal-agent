"""Ход берёт тред явно в начале прогона (спека сервера, 13.1).

Чужая живая аренда — ``ThreadBusy`` до любой работы: цикл агента не
вызывается, модель запроса не получает. Аренда отпускается и в конце хода, и
при сбое подготовки — иначе тред держался бы до истечения срока.
"""

import importlib
import types

import pytest
from gc_core.threads.errors import ThreadBusy
from gc_core.threads.facade import Threads
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_core.threads.session_port import SessionPort
from gc_testkit.settings import make_settings

KEY = ThreadKey(system="cli", thread="t")


def _cfg(tmp_path):
    return make_settings(
        {
            "paths.threads_data_path": str(tmp_path),
            "backend.api_mode": "chat_completions",
            "backend.completions_url": "http://test",
            "agent.model": "x",
            "agent.max_steps": 1,
            "agent.system_prompt_id": "v1",
            "logging.save_logs": False,
        }
    )


async def _executor(monkeypatch, tmp_path, calls: list):
    from gc_core.run_context import acreate_run_context

    ae = importlib.import_module("gc_core.agent_executor")
    monkeypatch.setattr(ae, "gc", types.SimpleNamespace(collect=lambda: None))

    async def _stub(messages, state, ctx, **kw):
        calls.append(messages)
        return {"finished": True}

    monkeypatch.setattr(ae, "arun_agent", _stub)
    ctx = await acreate_run_context(_cfg(tmp_path), auth_connector=None)
    return ae.AgentExecutor(ctx)


async def _other_takes(tmp_path) -> SessionPort:
    other = SessionPort(FileThreadStore(tmp_path), owner="other/1/abcdef12")
    await other.aclaim(KEY.system, KEY.thread)
    return other


async def test_busy_thread_refuses_before_the_loop(monkeypatch, tmp_path):
    calls: list = []
    ex = await _executor(monkeypatch, tmp_path, calls)
    await _other_takes(tmp_path)
    with pytest.raises(ThreadBusy) as busy:
        await ex.arun(["сделай"], thread_id=KEY.thread, system=KEY.system)
    assert busy.value.owner == "other/1/abcdef12"
    assert calls == []


async def test_run_after_release_goes_and_frees_the_thread(
    monkeypatch, tmp_path
):
    calls: list = []
    ex = await _executor(monkeypatch, tmp_path, calls)
    other = await _other_takes(tmp_path)
    await other.arelease_lease(KEY.system, KEY.thread)
    await ex.arun(["сделай"], thread_id=KEY.thread, system=KEY.system)
    assert len(calls) == 1
    # Ход кончился — тред свободен для другого владельца.
    await _other_takes(tmp_path)


async def test_setup_failure_releases_the_lease(monkeypatch, tmp_path):
    calls: list = []
    ex = await _executor(monkeypatch, tmp_path, calls)

    async def _broken(*_a, **_kw):
        raise RuntimeError("сборка состояния упала")

    monkeypatch.setattr(ex, "_abuild_run_state", _broken)
    with pytest.raises(RuntimeError):
        await ex.arun(["сделай"], thread_id=KEY.thread, system=KEY.system)
    await _other_takes(tmp_path)


async def test_facade_write_does_not_block_the_next_run(monkeypatch, tmp_path):
    """Посев истории фасадом (chat completions, техсообщение TUI) перед ходом
    не держит тред: ход берёт его сразу."""
    calls: list = []
    ex = await _executor(monkeypatch, tmp_path, calls)
    await Threads(FileThreadStore(tmp_path)).append_messages(
        KEY, [{"id": "seed", "role": "user", "content": "до хода"}]
    )
    await ex.arun(["сделай"], thread_id=KEY.thread, system=KEY.system)
    assert len(calls) == 1


async def _turn(tmp_path):
    record = await FileThreadStore(tmp_path).get_thread(KEY)
    return record.runtime.turn if record else None


async def test_turn_is_marked_running_while_the_run_goes(
    monkeypatch, tmp_path
):
    """Отметка хода — для восстановления после падения: пока ход идёт,
    ``runtime.turn == "running"``; после штатного конца отметки нет (meta
    треда — как раньше)."""
    seen: list = []
    ex = await _executor(monkeypatch, tmp_path, seen)
    import gc_core.agent_executor as ae

    real_loop = ae.arun_agent

    async def peek(messages, state, ctx, **kw):
        seen.append(await _turn(tmp_path))
        return await real_loop(messages, state, ctx, **kw)

    monkeypatch.setattr(ae, "arun_agent", peek)
    await ex.arun(["сделай"], thread_id=KEY.thread, system=KEY.system)
    assert seen[0] == "running"
    assert await _turn(tmp_path) is None


async def test_failed_run_clears_the_mark(monkeypatch, tmp_path):
    calls: list = []
    ex = await _executor(monkeypatch, tmp_path, calls)
    import gc_core.agent_executor as ae

    async def broken(*_a, **_kw):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr(ae, "arun_agent", broken)
    with pytest.raises(RuntimeError):
        await ex.arun(["сделай"], thread_id=KEY.thread, system=KEY.system)
    assert await _turn(tmp_path) is None


async def test_cancelled_run_keeps_the_mark_for_recovery(
    monkeypatch, tmp_path
):
    """Снятая задача (остановка сервера) — не отказ: отметка остаётся, и
    восстановление продолжит ход после перезапуска."""
    import asyncio

    calls: list = []
    ex = await _executor(monkeypatch, tmp_path, calls)
    import gc_core.agent_executor as ae

    started = asyncio.Event()

    async def hang(*_a, **_kw):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ae, "arun_agent", hang)
    task = asyncio.create_task(
        ex.arun(["сделай"], thread_id=KEY.thread, system=KEY.system)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _turn(tmp_path) == "running"


async def test_claim_hook_runs_after_the_thread_is_taken(
    monkeypatch, tmp_path
):
    """Фронтенд узнаёт, что ход взял тред (статус задачи вебхука пишет только
    взявший), — до чтения истории и цикла."""
    calls: list = []
    ex = await _executor(monkeypatch, tmp_path, calls)
    claimed: list = []

    async def on_claimed(system_id, thread_id):
        owner = await Threads(FileThreadStore(tmp_path)).busy_owner(KEY)
        claimed.append((system_id, thread_id, owner is not None))

    ex._ctx.on_thread_claimed = on_claimed
    await ex.arun(["сделай"], thread_id=KEY.thread, system=KEY.system)
    assert claimed == [(KEY.system, KEY.thread, True)]
