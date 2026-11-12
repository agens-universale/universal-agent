"""Отмена хода через поток ``control`` треда (спека сервера, 13.5).

Отмену может прислать любой процесс: запись ``{"type": "cancel"}`` в поток
треда. Идущий ход видит её тем же опросом, что и входящий поток, и
останавливается на следующем шаге; запись, сделанная до старта хода, его не
касается.
"""

import asyncio

from gc_core.threads.facade import Threads
from gc_core.threads.file_store import FileThreadStore
from test_inbox import (
    KEY,
    THINK,
    _executor,
    _finish,
    _history_count,
    _inbox_seq,
    _install,
    _mentions,
)
from gc_testkit import scripted_model


async def test_cancel_from_another_process_stops_the_run(
    monkeypatch, tmp_path
):
    holder: dict = {}

    async def plan(index, _payload):
        if index == 0:
            await Threads(FileThreadStore(tmp_path)).request_cancel(KEY)
            for _ in range(500):
                state = holder["ex"].get_state()
                if state is not None and state.stop_requested:
                    break
                await asyncio.sleep(0.01)
            return THINK
        return scripted_model.FINISH

    seen = _install(monkeypatch, plan)
    holder["ex"] = ex = await _executor(tmp_path)
    await ex.arun(["долгая задача"], thread_id=KEY.thread, system=KEY.system)
    assert len(seen) == 1
    assert ex.get_state().stop_requested


async def test_cancel_before_the_run_does_not_touch_it(monkeypatch, tmp_path):
    seen = _install(monkeypatch, _finish)
    receipt = await Threads(FileThreadStore(tmp_path)).request_cancel(KEY)
    assert receipt.max_seq["control"] == 1
    ex = await _executor(tmp_path)
    await ex.arun(["сделай"], thread_id=KEY.thread, system=KEY.system)
    assert len(seen) == 1
    assert not ex.get_state().stop_requested


async def test_cancel_carries_the_inbox_boundary(tmp_path):
    threads = Threads(FileThreadStore(tmp_path))
    await threads.submit(KEY, {"role": "user", "content": "первое"})
    await threads.request_cancel(KEY)
    entries = await threads.store.read_stream(KEY, "control")
    assert entries[-1].data["upto_inbox_seq"] == 1


async def test_cancel_while_the_turn_is_being_built_stops_it(
    monkeypatch, tmp_path
):
    """Находка ревью №3: отмена пришла, пока ход собирался (сообщение уже во
    входящем потоке, ход ещё не читал ``control``). Ход по этому сообщению
    останавливается, сообщение снято: в историю не попадает, отметка
    доставки продвинута до границы отмены."""
    seen = _install(monkeypatch, _finish)
    threads = Threads(FileThreadStore(tmp_path))
    await threads.submit(KEY, {"role": "user", "content": "отменённое"})
    await threads.request_cancel(KEY)
    ex = await _executor(tmp_path)
    await ex.arun([], thread_id=KEY.thread, system=KEY.system)
    assert seen == []
    assert ex.get_state() is None or ex.get_state().stop_requested
    assert await _inbox_seq(tmp_path) == 1
    assert await _history_count(tmp_path, "отменённое") == 0


async def test_cancel_does_not_touch_a_turn_for_a_later_message(
    monkeypatch, tmp_path
):
    seen = _install(monkeypatch, _finish)
    threads = Threads(FileThreadStore(tmp_path))
    await threads.submit(KEY, {"role": "user", "content": "отменённое"})
    await threads.request_cancel(KEY)
    await (await _executor(tmp_path)).arun(
        [], thread_id=KEY.thread, system=KEY.system
    )
    await threads.submit(KEY, {"role": "user", "content": "следующее"})
    ex = await _executor(tmp_path)
    await ex.arun([], thread_id=KEY.thread, system=KEY.system)
    assert len(seen) == 1
    assert _mentions(seen[0], "следующее") == 1
    assert _mentions(seen[0], "отменённое") == 0
    assert not ex.get_state().stop_requested
    assert await _inbox_seq(tmp_path) == 2


async def test_message_after_the_cancel_is_played_in_the_same_turn(
    monkeypatch, tmp_path
):
    """Отмена снимает сообщения до своей границы; пришедшее после неё —
    уже новая работа: ход её делает сам, не дожидаясь восстановления."""
    seen = _install(monkeypatch, _finish)
    threads = Threads(FileThreadStore(tmp_path))
    await threads.submit(KEY, {"role": "user", "content": "отменённое"})
    await threads.request_cancel(KEY)
    await threads.submit(KEY, {"role": "user", "content": "следующее"})
    ex = await _executor(tmp_path)
    await ex.arun([], thread_id=KEY.thread, system=KEY.system)
    assert len(seen) == 1
    assert _mentions(seen[0], "следующее") == 1
    assert _mentions(seen[0], "отменённое") == 0
    assert await _inbox_seq(tmp_path) == 2


async def test_cancel_aimed_at_the_turn_stops_it_while_it_is_built(
    monkeypatch, tmp_path
):
    """Отмена ответа chat или задачи A2A с другого инстанса называет свою
    цель: ход с этой целью останавливается, даже если отмена легла, пока он
    собирался (сообщения хода — не входящий поток, границы у неё нет)."""
    seen = _install(monkeypatch, _finish)
    threads = Threads(FileThreadStore(tmp_path))
    await threads.request_cancel(KEY, target="resp_1")
    ex = await _executor(tmp_path)
    await ex.arun(
        ["сделай"],
        thread_id=KEY.thread,
        system=KEY.system,
        cancel_target="resp_1",
    )
    assert seen == []
    other = await _executor(tmp_path)
    await other.arun(
        ["сделай"],
        thread_id=KEY.thread,
        system=KEY.system,
        cancel_target="resp_2",
    )
    assert len(seen) == 1
