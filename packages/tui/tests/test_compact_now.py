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
