"""Отмена хода через поток ``control`` треда (спека сервера, 13.5).

Отмену может прислать любой процесс: запись ``{"type": "cancel"}`` в поток
треда. Идущий ход видит её тем же опросом, что и входящий поток, и
останавливается на следующем шаге; запись, сделанная до старта хода, его не
касается.
"""

import asyncio

from gc_core.threads.facade import Threads
from gc_core.threads.file_store import FileThreadStore
from test_inbox import KEY, THINK, _executor, _install, _finish
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
