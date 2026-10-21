"""Снятие сообщения из очереди исполнителя (спека P3 §2.4).

Сообщение, поставленное в очередь во время хода, человек может забрать, пока
агент его не впустил. Под замком исполнителя сообщение либо снято, либо
впущено — не потеряно и не задвоено. Технический спутник реплики (подвал
``@path``) связан производным ``id`` и уходит вместе с ней.
"""

from __future__ import annotations

import asyncio
import threading
import time

from gc_core.agent_executor import AgentExecutor
from gc_core.execution.message_keys import companion_message_id
from gc_core.mcp_session_owner import set_owner
from gc_testkit.settings import fake_ctx


async def _no_cleanup() -> None:
    return None


def _executor() -> AgentExecutor:
    return AgentExecutor(fake_ctx(mcp_cleanup=_no_cleanup))


def _msg(msg_id: str) -> dict:
    return {"role": "user", "content": msg_id, "id": msg_id}


def _ids(messages: list[dict]) -> list[str]:
    return [m["id"] for m in messages]


def test_companion_id_is_derived_from_message_id():
    assert companion_message_id("m1") == "m1_t"


def test_withdraw_from_pending_keeps_neighbours_in_order():
    ex = _executor()
    for msg_id in ("a", "b", "c"):
        ex.add_message(_msg(msg_id))

    assert ex.withdraw_message("b") is True
    assert _ids(ex._take_pending_messages()) == ["a", "c"]


def test_withdraw_takes_own_companion_and_leaves_foreign_one():
    ex = _executor()
    ex.add_message(_msg("a"))
    ex.add_message(_msg(companion_message_id("a")))
    ex.add_message(_msg("b"))
    ex.add_message(_msg(companion_message_id("b")))

    assert ex.withdraw_message("a") is True
    assert _ids(ex._take_pending_messages()) == [
        "b",
        companion_message_id("b"),
    ]


def test_withdraw_after_admission_is_refused():
    ex = _executor()
    ex.add_message(_msg("a"))
    ex._take_pending_messages()

    assert ex.withdraw_message("a") is False


def test_withdraw_unknown_id_is_refused():
    ex = _executor()
    ex.add_message(_msg("a"))

    assert ex.withdraw_message("zzz") is False
    assert _ids(ex._take_pending_messages()) == ["a"]


def test_withdraw_from_undelivered_tail():
    ex = _executor()
    ex.add_message(_msg("a"))
    ex.add_message(_msg(companion_message_id("a")))
    ex.add_message(_msg("b"))

    async def _teardown() -> None:
        await ex._ateardown_run(set_owner(None))

    asyncio.run(_teardown())

    assert ex.withdraw_message("a") is True
    assert _ids(ex.take_undelivered_messages()) == ["b"]


def _race(ex: AgentExecutor, rounds: int) -> tuple[list[str], list[str]]:
    """Раунд: сообщение в очереди, затем оба потока разом бросаются на
    него — один впускает, другой снимает."""
    admitted: list[str] = []
    withdrawn: list[str] = []
    queued = threading.Barrier(2, timeout=10)

    def _admit() -> None:
        for i in range(rounds):
            ex.add_message(_msg(f"m{i}"))
            queued.wait()
            admitted.extend(_ids(ex._take_pending_messages()))

    def _withdraw() -> None:
        for i in range(rounds):
            queued.wait()
            if i % 2:
                time.sleep(0)  # уступить ход: исходы раунда разные
            if ex.withdraw_message(f"m{i}"):
                withdrawn.append(f"m{i}")

    workers = [
        threading.Thread(target=_admit),
        threading.Thread(target=_withdraw),
    ]
    for t in workers:
        t.start()
    for t in workers:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in workers)
    admitted.extend(_ids(ex._take_pending_messages()))
    return admitted, withdrawn


def test_withdraw_races_admission_without_loss_or_duplicate():
    rounds = 500
    admitted, withdrawn = _race(_executor(), rounds)

    assert sorted(admitted + withdrawn) == sorted(
        f"m{i}" for i in range(rounds)
    )
    assert not set(admitted) & set(withdrawn)
