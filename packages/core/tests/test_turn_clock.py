"""Часы хода: длительность без ожидания человека (P1b, Task 2)."""

import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from gc_core.execution.environment import execution_environment
from gc_core.execution.turn_clock import TurnClock, turn_clock_sources

_T0 = datetime(2026, 10, 22, 11, 30, 53, 4000, tzinfo=UTC)


class _Mono:
    """Подменная монотоника: время двигает тест."""

    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _Wall:
    """Подменная стена: тот же сдвиг, что у монотоники."""

    def __init__(self, mono: _Mono) -> None:
        self._mono = mono
        self._base = mono.now

    def __call__(self) -> datetime:
        return _T0 + timedelta(seconds=self._mono.now - self._base)


def _clock() -> tuple[TurnClock, _Mono]:
    mono = _Mono()
    return TurnClock.start(mono=mono, wall=_Wall(mono)), mono


def test_elapsed_without_waits_is_time_since_start():
    clock, mono = _clock()
    mono.now += 7.5
    assert clock.elapsed() == 7.5


def test_one_wait_is_subtracted():
    clock, mono = _clock()
    mono.now += 2
    clock.begin_wait()
    mono.now += 5
    clock.end_wait()
    mono.now += 3
    assert clock.elapsed() == 5


def test_open_wait_does_not_grow_elapsed():
    clock, mono = _clock()
    mono.now += 2
    clock.begin_wait()
    mono.now += 30
    assert clock.elapsed() == 2


def test_overlapping_waits_count_as_union():
    clock, mono = _clock()
    mono.now += 1
    clock.begin_wait()
    mono.now += 2
    clock.begin_wait()
    mono.now += 3
    clock.end_wait()
    mono.now += 4
    clock.end_wait()
    mono.now += 1
    assert clock.elapsed() == 2


def test_close_during_wait_cuts_wait_at_close():
    clock, mono = _clock()
    mono.now += 4
    clock.begin_wait()
    mono.now += 6
    clock.close()
    mono.now += 10
    clock.end_wait()
    assert clock.closed is True
    assert clock.elapsed() == 4


def test_second_close_changes_nothing():
    clock, mono = _clock()
    mono.now += 3
    clock.close()
    ended_at = clock.ended_at
    mono.now += 9
    clock.close()
    assert clock.elapsed() == 3
    assert clock.ended_at == ended_at


def test_elapsed_after_close_does_not_grow():
    clock, mono = _clock()
    mono.now += 3
    clock.close()
    mono.now += 100
    clock.begin_wait()
    mono.now += 5
    clock.end_wait()
    assert clock.elapsed() == 3


def test_effective_start_is_start_plus_wait():
    clock, mono = _clock()
    start = mono.now
    mono.now += 1
    clock.begin_wait()
    mono.now += 5
    clock.end_wait()
    assert clock.effective_start() == start + 5
    mono.now += 2
    assert mono.now - clock.effective_start() == clock.elapsed()


def test_live_count_from_effective_start_stands_during_wait():
    clock, mono = _clock()
    mono.now += 2
    clock.begin_wait()
    mono.now += 8
    assert mono.now - clock.effective_start() == 2


def test_wall_stamps_are_utc_iso_with_z():
    clock, mono = _clock()
    assert clock.started_at == "2026-10-22T11:30:53.004Z"
    assert clock.ended_at is None
    mono.now += 72.125
    clock.close()
    assert clock.ended_at == "2026-10-22T11:32:05.129Z"


def test_recorded_starts_false():
    clock, _ = _clock()
    assert clock.recorded is False


def test_waits_from_two_threads_do_not_race():
    for _ in range(200):
        clock, mono = _clock()
        clock.begin_wait()
        mono.now += 1
        ender = threading.Thread(target=clock.end_wait)
        beginner = threading.Thread(target=clock.begin_wait)
        ender.start()
        beginner.start()
        ender.join()
        beginner.join()
        clock.end_wait()
        mono.now += 1
        assert clock.elapsed() == 1


def test_sources_take_operations_monotonic():
    ops = SimpleNamespace(monotonic=lambda: 42.0)
    mono, _ = turn_clock_sources(SimpleNamespace(operations=ops))
    assert mono() == 42.0


def test_sources_take_wall_from_execution_environment():
    fixed = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    _, wall = turn_clock_sources(SimpleNamespace())
    with execution_environment(new_id=lambda p: p, now=lambda: fixed):
        assert wall() == fixed


# ---- время вызовов хода (P4: карта `calls` записи итога) -----------------


def test_record_call_lands_in_calls_rounded():
    clock, _ = _clock()
    clock.record_call("c1", 1.26)
    clock.record_call("c2", 0.04)

    assert clock.calls() == {"c1": 1.3, "c2": 0.0}


def test_calls_returns_a_copy():
    clock, _ = _clock()
    clock.record_call("c1", 2.0)
    clock.calls()["c1"] = 99.0

    assert clock.calls() == {"c1": 2.0}


def test_record_call_after_close_is_noop():
    clock, _ = _clock()
    clock.close()
    clock.record_call("c1", 2.0)

    assert clock.calls() == {}


def test_record_call_without_call_id_is_noop():
    clock, _ = _clock()
    clock.record_call("", 2.0)

    assert clock.calls() == {}


def test_waited_grows_while_wait_is_open():
    clock, mono = _clock()
    clock.begin_wait()
    mono.now += 4
    assert clock.waited() == 4
    mono.now += 6
    assert clock.waited() == 10


def test_waited_is_fixed_after_end_wait():
    clock, mono = _clock()
    clock.begin_wait()
    mono.now += 4
    clock.end_wait()
    mono.now += 6

    assert clock.waited() == 4


# ---- время вызова для живого счёта фронтенда -----------------------------


def test_call_elapsed_counts_from_call_start():
    clock, mono = _clock()
    mono.now += 3
    clock.begin_call("c1")
    mono.now += 12.5
    assert clock.call_elapsed("c1") == 12.5


def test_call_elapsed_after_record_is_recorded_rounded():
    """Законченный вызов — то же время, что войдёт в запись итога хода."""
    clock, mono = _clock()
    clock.begin_call("c1")
    mono.now += 13.04
    clock.record_call("c1", 13.04)
    mono.now += 5
    assert clock.call_elapsed("c1") == clock.calls()["c1"] == 13.0


def test_call_elapsed_stands_during_human_wait():
    clock, mono = _clock()
    clock.begin_call("c1")
    mono.now += 2
    clock.begin_wait()
    mono.now += 30
    assert clock.call_elapsed("c1") == 2
    clock.end_wait()
    mono.now += 1
    assert clock.call_elapsed("c1") == 3


def test_wait_before_call_start_is_not_subtracted():
    clock, mono = _clock()
    clock.begin_wait()
    mono.now += 30
    clock.end_wait()
    assert clock.begin_call("c1") == 30
    mono.now += 4
    assert clock.call_elapsed("c1") == 4


def test_call_elapsed_of_unknown_call_is_none():
    clock, _ = _clock()
    assert clock.call_elapsed("c1") is None
