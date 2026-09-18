"""Хвост прогона TUI доигрывается своим ходом в том же треде.

TUI держит один исполнитель на сессию. Сообщение, набранное на излёте хода —
после последнего забора очереди циклом, пока прогон сносит MCP, — оставалось в
очереди исполнителя: на экране оно висело отправленным, а до агента доезжало
только со СЛЕДУЮЩИМ запросом пользователя, позади него и одним с ним батчем.

Исполнитель настоящий — `AgentExecutor` с его очередью; подменены только цикл
агента (записывает батчи) и снос MCP (двойник выполняет шаг теста ровно в
окне сноса).
"""
import asyncio
import importlib
import types

import pytest

from gc_tui.services.agent_runner import AgentRunner

_WAIT_SEC = 10.0


def _cfg(tmp_path):
    return {
        "threads_data_path": str(tmp_path),
        "backend_api_mode": "chat_completions",
        "completions_url": "http://test",
        "model": "x",
        "max_steps": 1,
        "system_prompt_id": "v1",
    }


@pytest.fixture()
def env(monkeypatch, tmp_path):
    from gc_core.run_context import acreate_run_context

    ae = importlib.import_module("gc_core.agent_executor")
    monkeypatch.setattr(ae, "gc", types.SimpleNamespace(collect=lambda: None))
    h = types.SimpleNamespace(batches=[], runs=[], teardown_steps=[])

    async def _loop(messages, state, ctx, **kw):
        h.batches.append(
            [m.get("content") if isinstance(m, dict) else m for m in messages]
        )
        return {"finished": True}

    original_teardown = ae._aclose_mcp_and_release

    async def _teardown(owner_reset):
        if h.teardown_steps:
            h.teardown_steps.pop(0)()
        await original_teardown(owner_reset)

    monkeypatch.setattr(ae, "arun_agent", _loop)
    monkeypatch.setattr(ae, "_aclose_mcp_and_release", _teardown)
    ctx = asyncio.run(acreate_run_context(_cfg(tmp_path), auth_connector=None))
    executor = ae.AgentExecutor(ctx)
    arun = executor.arun

    async def _recording_arun(messages, **kw):
        h.runs.append((kw.get("thread_id"), kw.get("start_new_thread")))
        return await arun(messages, **kw)

    executor.arun = _recording_arun
    h.executor = executor
    return h


def _user(text: str) -> dict:
    return {"role": "user", "content": text, "id": f"m_{text}"}


def _finish(runner: AgentRunner) -> None:
    runner.wait(timeout=_WAIT_SEC)
    assert not runner.is_running(), "прогон не закончился"


def test_message_typed_during_teardown_gets_its_own_turn(env):
    runner = AgentRunner(executor=env.executor)
    env.teardown_steps.append(lambda: runner.add_message(_user("вдогонку")))

    assert runner.start(query="первый", thread_id="t", start_new_thread=True)
    _finish(runner)
    assert runner.start(query="второй", thread_id="t")
    _finish(runner)

    assert env.batches == [["первый"], ["вдогонку"], ["второй"]], (
        f"хвост прошлого хода уехал в следующий запрос пользователя: {env.batches}"
    )
    assert env.runs[1] == ("t", False), "хвост доигран не в том же треде"


def test_message_that_misses_the_closed_run_is_not_mixed_into_the_next_request(env):
    """Опоздавшее к закрытию прогона сообщение уходит своим ходом, не чужим.

    UI спрашивает `is_running()` и лишь потом отдаёт сообщение — между этими
    вызовами прогон успевает закрыться. Для раннера это ровно состояние после
    конца прогона, его тест и воспроизводит.
    """
    runner = AgentRunner(executor=env.executor)
    assert runner.start(query="первый", thread_id="t", start_new_thread=True)
    _finish(runner)

    runner.add_message(_user("опоздавшее"))
    _finish(runner)
    assert runner.start(query="второй", thread_id="t")
    _finish(runner)

    assert env.batches == [["первый"], ["опоздавшее"], ["второй"]], (
        f"опоздавшее сообщение уехало в следующий запрос пользователя: {env.batches}"
    )
    assert env.runs[1] == ("t", False)


def test_stopped_run_neither_replays_its_tail_nor_passes_it_on(env):
    """После стопа хвост не доигрывается — и не уезжает в следующий запрос."""
    runner = AgentRunner(executor=env.executor)

    def _typed_then_stopped():
        runner.add_message(_user("вдогонку"))
        runner.stop()

    env.teardown_steps.append(_typed_then_stopped)
    assert runner.start(query="первый", thread_id="t", start_new_thread=True)
    _finish(runner)
    assert runner.start(query="второй", thread_id="t")
    _finish(runner)

    assert env.batches == [["первый"], ["второй"]], (
        f"хвост остановленного хода доигран или уехал дальше: {env.batches}"
    )


def test_message_during_compaction_does_not_start_a_parallel_run():
    """Пока идёт `/compact`, опоздавшее сообщение не заводит прогон рядом с ним.

    UI видит живой поток сжатия как `is_running()` и отдаёт сообщение раннеру.
    Отдельный ход здесь запустил бы второй воркер на том же executor и треде
    параллельно сжатию.
    """
    import threading

    class _Executor:
        def __init__(self):
            self.runs: list = []
            self.queued: list = []
            self.compacting = threading.Event()
            self.release = threading.Event()

        async def arun(self, messages, **kw):
            self.runs.append(list(messages))

        async def acompact_thread(self, thread_id, system=None):
            self.compacting.set()
            self.release.wait(timeout=_WAIT_SEC)

        def add_message(self, message):
            self.queued.append(message)

        def stop_run(self):
            pass

    executor = _Executor()
    runner = AgentRunner(executor=executor)
    assert runner.start(query="первый", thread_id="t")
    _finish(runner)
    assert runner.compact_now("t", "cli")
    assert executor.compacting.wait(_WAIT_SEC)
    try:
        runner.add_message(_user("во время сжатия"))
        assert executor.runs == [["первый"]], (
            f"сообщение завело прогон параллельно сжатию: {executor.runs}"
        )
        assert executor.queued == [_user("во время сжатия")]
    finally:
        executor.release.set()
        runner.wait(timeout=_WAIT_SEC)
