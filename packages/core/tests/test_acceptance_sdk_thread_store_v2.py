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


async def test_lease_is_released_after_the_turn(tmp_path, monkeypatch):
    """Аренда — на время хода: после него тред свободен для другого
    инстанса (раньше она висела до истечения срока)."""
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(list(TURNS)))
    store = MemoryThreadStore()
    await _run(cfg, store)
    assert (await store.get_thread(KEY)).lease is None


async def test_lease_is_renewed_while_the_turn_runs(tmp_path, monkeypatch):
    """Шаг дольше срока аренды не отдаёт тред: пока идёт ход, аренда
    продлевается в фоне."""
    from gc_core import agent_executor

    class Counting(MemoryThreadStore):
        claims = 0

        async def claim(self, key, lease):
            Counting.claims += 1
            return await super().claim(key, lease)

    from gc_core import processing_responses as pr
    from helpers.turn_finish_apply import stub_turn_finish_apply

    async def slow_step(**kwargs):
        if kwargs.get("tool_name") == "exec_sleep":
            await asyncio.sleep(0.2)
        return await stub_turn_finish_apply(**kwargs)

    monkeypatch.setattr(pr, "aapply_tool", slow_step)
    monkeypatch.setattr(agent_executor, "LEASE_RENEW_SECONDS", 0.005)
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(list(TURNS)))
    store = Counting()
    await _run(cfg, store)
    assert Counting.claims > 3, "аренда не продлевалась во время хода"
    assert (await store.get_thread(KEY)).lease is None


async def test_second_instance_continues_the_thread_right_away(
    tmp_path, monkeypatch
):
    """Два инстанса (разные хосты) на одном каталоге тредов: второй ведёт
    тред сразу после хода первого, без «тред ведёт другой»."""
    from gc_core.threads.file_store import FileThreadStore
    from gc_core.threads.instance import INSTANCE_ENV

    cfg = parity_config(tmp_path, monkeypatch)
    threads = tmp_path / "shared-threads"
    for host in ("host-a", "host-b"):
        monkeypatch.setenv(INSTANCE_ENV, host)
        install(monkeypatch, ScriptedBackend(list(TURNS)))
        assert await _run(cfg, FileThreadStore(threads), f"ход {host}")
    history = await FileThreadStore(threads).read_stream(KEY, "messages")
    texts = [str(e.data.get("content")) for e in history]
    assert any("ход host-a" in t for t in texts)
    assert any("ход host-b" in t for t in texts)


async def test_store_refusal_fails_the_turn_instead_of_losing_steps(
    tmp_path, monkeypatch
):
    """Хранилище отказало в записи шага (тред ведёт другой инстанс) — ход
    падает, а не отвечает пользователю с дырами в истории."""
    from gc_core.threads.errors import OwnershipError

    class Refusing(MemoryThreadStore):
        async def commit(self, key, change):
            if change.append_messages:
                raise OwnershipError("тред ведёт 'other-host'")
            return await super().commit(key, change)

    from gc_core.execution.contracts import ExecutionFailure

    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(list(TURNS)))
    with pytest.raises(ExecutionFailure, match="other-host") as failure:
        await _run(cfg, Refusing())
    assert isinstance(failure.value.__cause__, OwnershipError)
