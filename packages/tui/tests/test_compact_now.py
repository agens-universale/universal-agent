"""compact_now: off-thread форс-сжатие, отказ если worker занят."""

import time

from gc_tui.services.agent_runner import AgentRunner


def test_compact_now_runs_off_thread():
    seen = {}

    class _Ex:
        async def acompact_thread(self, tid, system=None):
            seen["args"] = (tid, system)
            return True

    # AgentRunner(executor=...) — executor задаётся напрямую (см. ctor: executor /
    # executor_factory / on_error). _ensure_executor вернёт этот executor.
    runner = AgentRunner(executor=_Ex())
    assert runner.compact_now("t1", "cli") is True
    for _ in range(100):
        if "args" in seen:
            break
        time.sleep(0.01)
    assert seen["args"] == ("t1", "cli")


def test_compact_now_rejects_when_busy():
    import threading

    class _Ex:
        async def acompact_thread(self, tid, system=None):
            return True

    runner = AgentRunner(executor=_Ex())
    runner._thread = threading.Thread(target=lambda: time.sleep(0.5))
    runner._thread.start()
    try:
        assert runner.compact_now("t1", "cli") is False
    finally:
        runner._thread.join()


def test_compact_now_targets_its_own_thread():
    """Сжатие — работа своего треда: цель раннера — он, а не тред прошлого
    хода (иначе ошибка сжатия «чужая» и реплика после него ушла бы
    отдельным ходом в прошлый тред)."""

    class _Ex:
        async def acompact_thread(self, tid, system=None):
            return True

    runner = AgentRunner(executor=_Ex())
    runner._last_target = ("tA", "cli")
    assert runner.compact_now("tB", "cli") is True
    assert runner._last_target == ("tB", "cli")
    runner._thread.join()


def test_compact_now_bumps_run_seq():
    """`/compact` — свой прогон раннера: номер прогона растёт, как у хода.
    Иначе сжатие сразу после зафиксированного прогона совпало бы с его
    парой (раннер, номер) и осталось бы без бара (S10 §3.7)."""

    class _Ex:
        async def acompact_thread(self, tid, system=None):
            return True

    runner = AgentRunner(executor=_Ex())
    before = runner.run_seq
    assert runner.compact_now("t1", "cli") is True
    runner._thread.join()
    assert runner.run_seq == before + 1
