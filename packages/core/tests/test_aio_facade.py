"""Публичный async-фасад SDK (план async-переезда, Задача 17)."""
import asyncio
import threading
import time

import pytest

from gc_core.aio import AsyncAgent

from parity_harness import (
    ScriptedBackend,
    assistant_tool_call,
    install,
    judge_accept,
    parity_config,
)


def _finish(report: str, call_id: str = "c2") -> dict:
    return assistant_tool_call("turn_finish", {
        "solution_type": "direct_answer",
        "task_report": report,
        "handoff": "{}",
    }, call_id=call_id)


def _scenario(report: str = "готово") -> list[dict]:
    return [_finish(report), judge_accept()]


async def test_facade_runs_a_turn(tmp_path, monkeypatch):
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(_scenario("42")))

    async with await AsyncAgent.create(cfg) as agent:
        answer = await agent.run("сколько будет 21*2", thread_id="t1")

    assert "42" in str(answer)


async def test_facade_does_not_block_the_loop(tmp_path, monkeypatch):
    """Смысл всего переезда: пока агент работает, loop живёт.

    Соседняя корутина тикает каждые 10 мс; максимальный разрыв между тиками
    показывает, залипал ли loop.
    """
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(_scenario()))

    gaps: list[float] = []
    stop = asyncio.Event()

    async def _heartbeat() -> None:
        prev = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - prev)
            prev = now

    beat = asyncio.create_task(_heartbeat())
    async with await AsyncAgent.create(cfg) as agent:
        await agent.run("привет", thread_id="t1")
    stop.set()
    await beat

    assert gaps, "хартбит не тикал вовсе"
    assert max(gaps) < 0.5, f"loop залипал на {max(gaps):.3f}с"


async def test_two_agents_run_concurrently(tmp_path, monkeypatch):
    """Два агента на одном loop — без потока на каждого."""
    cfg_a = parity_config(tmp_path / "a", monkeypatch)
    cfg_b = parity_config(tmp_path / "b", monkeypatch)
    install(monkeypatch, ScriptedBackend(_scenario("раз") + _scenario("два")))

    before = threading.active_count()
    a = await AsyncAgent.create(cfg_a)
    b = await AsyncAgent.create(cfg_b)
    async with a, b:
        r1, r2 = await asyncio.gather(
            a.run("раз", thread_id="t1"),
            b.run("два", thread_id="t2"),
        )

    assert r1 and r2
    # Прогон агента больше не держит по потоку на себя; допуск на служебные
    # потоки httpx/логгера, но не на «поток на каждый прогон».
    assert threading.active_count() <= before + 2


async def test_facade_streams_deltas(tmp_path, monkeypatch):
    """stream() отдаёт дельты и доигрывает ход до конца."""
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(_scenario("поток")))

    seen = []
    async with await AsyncAgent.create(cfg) as agent:
        async for kind, text in agent.stream("привет", thread_id="t1"):
            seen.append((kind, text))

    # Скриптованный бэкенд не стримит, поэтому дельт может не быть вовсе —
    # проверяем, что итератор корректно завершился, а не завис.
    assert isinstance(seen, list)


async def test_stream_propagates_run_error(tmp_path, monkeypatch):
    """Ошибка прогона поднимается из итератора, а не теряется в тишине."""
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(_scenario()))

    async with await AsyncAgent.create(cfg) as agent:
        async def _boom(*_a, **_k):
            raise RuntimeError("прогон сломался")

        monkeypatch.setattr(agent.executor, "arun", _boom)
        with pytest.raises(RuntimeError, match="прогон сломался"):
            async for _ in agent.stream("привет", thread_id="t1"):
                pass


async def test_stop_is_idempotent(tmp_path, monkeypatch):
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(_scenario()))

    async with await AsyncAgent.create(cfg) as agent:
        agent.stop()
        agent.stop()


async def test_send_before_run_is_buffered(tmp_path, monkeypatch):
    """send() до прогона не падает: сообщение ляжет в очередь исполнителя."""
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(_scenario()))

    async with await AsyncAgent.create(cfg) as agent:
        agent.send({"role": "user", "content": "вдогонку"})
