"""Приёмка SDK на хранилище контракта v2: агент работает, диска не касается,
а медленное хранилище не морозит цикл (спека хранилищ v2, 9.3).

Тот же стенд, что у ``test_acceptance_sdk_thread_store`` (старый контракт):
модель — ``ScriptedBackend``, диспетчер тулов заглушен целиком.
"""

import asyncio

import pytest
from gc_core.aio import AsyncAgent
from gc_core.threads.models import ThreadKey
from gc_core.threads.store import CONTRACT_METHODS
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore
from parity_harness import (
    ScriptedBackend,
    assistant_tool_call,
    install,
    judge_accept,
    parity_config,
)

KEY = ThreadKey(system="sdk", thread="t-sdk")

TURNS = [
    assistant_tool_call("exec_sleep", {"seconds": 0.05}, call_id="c1"),
    assistant_tool_call(
        "turn_finish",
        {
            "solution_type": "direct_answer",
            "task_report": "готово",
            "handoff": "{}",
        },
        call_id="c2",
    ),
    judge_accept(),
]


@pytest.fixture(autouse=True)
def _stub_turn_finish(monkeypatch):
    from gc_core import processing_responses as pr
    from helpers.turn_finish_apply import stub_turn_finish_apply

    monkeypatch.setattr(pr, "aapply_tool", stub_turn_finish_apply)


async def _run(cfg, store, text="сделай что-нибудь"):
    async with await AsyncAgent.create(cfg, thread_store=store) as agent:
        return await agent.run(text, thread_id="t-sdk")


async def test_sdk_run_on_v2_store_touches_no_thread_files(
    tmp_path, monkeypatch
):
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(list(TURNS)))
    store = MemoryThreadStore()
    assert await _run(cfg, store), "агент не ответил"
    thread_files = [
        p
        for p in tmp_path.rglob("*")
        if p.is_file() and p.name.startswith(("data_", "meta_"))
    ]
    assert thread_files == []
    record = await store.get_thread(KEY)
    assert record is not None and record.revision > 0
    messages = await store.read_stream(KEY, "messages")
    roles = [m.data.get("role") or m.data.get("type") for m in messages]
    assert "user" in roles, "запрос пользователя не сохранился"
    assert "function_call" in roles, "вызовы тулов не сохранились"
    assert record.runtime.flags == frozenset(), "флаги хода не сняты"


async def test_second_run_continues_v2_thread(tmp_path, monkeypatch):
    cfg = parity_config(tmp_path, monkeypatch)
    store = MemoryThreadStore()
    install(monkeypatch, ScriptedBackend(list(TURNS)))
    await _run(cfg, store, "первый")
    first = len(await store.read_stream(KEY, "messages"))
    backend = install(monkeypatch, ScriptedBackend(list(TURNS)))
    await _run(cfg, store, "второй")
    assert len(await store.read_stream(KEY, "messages")) > first
    sent = str(backend.calls[0])
    assert "первый" in sent, "история первого прогона не ушла в модель"


class _SlowStore(MemoryThreadStore):
    """Хранилище с сетевой задержкой 5 мс на каждый вызов контракта."""

    def __init__(self) -> None:
        super().__init__()
        self.awaited = 0


def _slow(name):
    real = getattr(MemoryThreadStore, name)

    async def method(self, *args, **kwargs):
        self.awaited += 1
        await asyncio.sleep(0.005)
        return await real(self, *args, **kwargs)

    method.__name__ = name
    return method


for _name in CONTRACT_METHODS:
    setattr(_SlowStore, _name, _slow(_name))


async def test_loop_stays_free_with_slow_v2_store(tmp_path, monkeypatch):
    from gc_testkit import (
        LoopPulse,
        assert_loop_was_free,
        quiet_gc_for_loop_timing,
    )

    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(list(TURNS)))
    store = _SlowStore()
    with quiet_gc_for_loop_timing():
        async with LoopPulse() as pulse:
            await _run(cfg, store)
    assert store.awaited >= 10, f"хранилище звали всего {store.awaited} раз"
    assert_loop_was_free(
        pulse.gaps,
        tick=pulse.step,
        label="хранилище v2 с RTT 5 мс: ",
        stall_after=0.050,
    )
