"""Часы хода: длительность работы агента без ожидания человека.

Часы — транзиентный объект прогона и живут на ``ctx`` рядом с ``turn_id``,
а не в ``State``: ожидания субагента обязаны лечь на ход родителя, а у
ребёнка ``State`` свой (``parent_state_id``), тогда как ``ctx`` он получает
от родителя тем же путём, что и ``turn_id``. В снимок ``State`` часы не идут
и не персистятся: персистентная истина — запись итога хода в треде.

Время — только из источников проекта (``turn_clock_sources``): ядро
исполняется и в коде workflow Temporal, где прямые часы запрещены.
"""

import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from gc_core.execution.environment import utc_now
from gc_core.execution.ports import port

Mono = Callable[[], float]
Wall = Callable[[], datetime]


def _utc_iso(moment: datetime) -> str:
    stamp = moment.astimezone(UTC).isoformat(timespec="milliseconds")
    return stamp.replace("+00:00", "Z")


class TurnClock:
    """Старт, ожидания человека и закрытие хода.

    Ожидания приходят из потоков пула и из ``OffLoop``, поэтому всё под
    замком. Пересекающиеся ожидания считаются объединением: отсчёт идёт от
    первого ``begin_wait`` до последнего ``end_wait``.
    """

    def __init__(self, *, mono: Mono, wall: Wall) -> None:
        self._mono = mono
        self._wall = wall
        self._lock = threading.Lock()
        self._started = mono()
        self._ended: float | None = None
        self._waited = 0.0
        self._depth = 0
        self._wait_from: float | None = None
        self.started_at = _utc_iso(wall())
        self.ended_at: str | None = None
        #: Запись итога хода уже начата (ставит писатель записи).
        self.recorded = False
        # Время завершившихся вызовов хода без ожидания человека. Карта —
        # транзиентная часть часов; персистентная истина — запись итога.
        self._calls: dict[str, float] = {}
        # Идущие вызовы: старт и ожидания хода на старте — живой счёт.
        self._open: dict[str, tuple[float, float]] = {}

    @classmethod
    def start(cls, *, mono: Mono, wall: Wall) -> "TurnClock":
        return cls(mono=mono, wall=wall)

    @property
    def closed(self) -> bool:
        return self._ended is not None

    def begin_wait(self) -> None:
        with self._lock:
            self._depth += 1
            if self._depth == 1 and self._ended is None:
                self._wait_from = self._mono()

    def end_wait(self) -> None:
        with self._lock:
            self._depth -= 1
            if self._depth == 0 and self._wait_from is not None:
                self._waited += self._mono() - self._wait_from
                self._wait_from = None

    def close(self) -> None:
        """Первый вызов побеждает; открытое ожидание обрезается им."""
        with self._lock:
            if self._ended is not None:
                return
            now = self._mono()
            if self._wait_from is not None:
                self._waited += now - self._wait_from
                self._wait_from = None
            self._ended = now
            self.ended_at = _utc_iso(self._wall())

    def waited(self) -> float:
        """Сумма ожиданий человека на «сейчас»: открытое — до текущего момента."""
        with self._lock:
            return self._waited_until(self._now())

    def begin_call(self, call_id: str) -> float:
        """Старт вызова (без ``call_id`` — не считается) → ожидания хода на
        старте: ожидания до вызова из его времени не вычитаются."""
        with self._lock:
            now = self._now()
            waited = self._waited_until(now)
            if self._ended is None and call_id:
                self._open[call_id] = (now, waited)
            return waited

    def record_call(self, call_id: str, seconds: float) -> None:
        """Время завершившегося вызова; после закрытия хода — ничего."""
        with self._lock:
            self._open.pop(call_id, None)
            if self._ended is None and call_id:
                self._calls[call_id] = seconds

    def call_elapsed(self, call_id: str) -> float | None:
        """Время вызова для живого счёта фронтенда: идущего — от старта без
        ожиданий человека; законченного — то же, что в ``calls()``."""
        with self._lock:
            if call_id in self._calls:
                return round(self._calls[call_id], 1)
            opened = self._open.get(call_id)
            if opened is None:
                return None
            start, waited = opened
            now = self._now()
            return now - start - (self._waited_until(now) - waited)

    def calls(self) -> dict[str, float]:
        """Копия карты ``{call_id: секунды}``, округление до 0,1."""
        with self._lock:
            return {k: round(v, 1) for k, v in self._calls.items()}

    def elapsed(self) -> float:
        """Время работы: до закрытия — по «сейчас»; ожидания вычтены."""
        with self._lock:
            end = self._now()
            return end - self._started - self._waited_until(end)

    def carry(self) -> dict[str, Any]:
        """Снимок хода на «сейчас» для продолжения другим прогоном: начало,
        время работы и вызовы (ход приостановлен на вопросе)."""
        return {
            "started_at": self.started_at,
            "elapsed": round(self.elapsed(), 1),
            "calls": self.calls(),
        }

    def resume(self, carried: Mapping[str, Any]) -> None:
        """Продолжить ход, перенесённый ``carry``: время работы, начало и
        вызовы прошлых прогонов — уже на часах."""
        with self._lock:
            self._started -= float(carried["elapsed"])
            self.started_at = str(carried["started_at"])
            self._calls = {**carried["calls"], **self._calls}

    def effective_start(self) -> float:
        """Старт, сдвинутый на ожидания: ``now − effective_start`` — живой
        счёт хода, и на открытом ожидании он стоит."""
        with self._lock:
            return self._started + self._waited_until(self._now())

    def _now(self) -> float:
        return self._ended if self._ended is not None else self._mono()

    def _waited_until(self, moment: float) -> float:
        if self._wait_from is None:
            return self._waited
        return self._waited + moment - self._wait_from


def turn_clock_sources(ctx: Any) -> tuple[Mono, Wall]:
    """Монотоника рантайма (порт ``operations``) и стена среды исполнения."""
    operations = port(ctx, "operations")
    mono = operations.monotonic if operations is not None else time.monotonic
    return mono, utc_now


def bind_turn_clock(ctx: Any, clock: TurnClock) -> None:
    """Часы текущего хода — на ctx (новый ход главного трейса)."""
    ctx.turn_clock = clock


def clear_turn_clock(ctx: Any) -> None:
    """Снять часы хода."""
    ctx.turn_clock = None


def current_turn_clock(ctx: Any) -> TurnClock | None:
    """Часы текущего хода, если привязаны."""
    clock = getattr(ctx, "turn_clock", None)
    return clock if isinstance(clock, TurnClock) else None


def begin_call_time(ctx: Any, call_id: str | None) -> float:
    """Старт вызова на часах хода → ожидания человека на его старте
    (для ``record_call_time``); часов нет — 0."""
    clock = current_turn_clock(ctx)
    return clock.begin_call(call_id or "") if clock is not None else 0.0


def record_call_time(
    ctx: Any, call_id: str | None, elapsed: float, waited_at_start: float
) -> None:
    """Время вызова — в часы хода, без ожиданий человека за время вызова
    (``waited_at_start`` — ``begin_call_time`` на его старте)."""
    clock = current_turn_clock(ctx)
    if clock is not None:
        waited = clock.waited() - waited_at_start
        clock.record_call(call_id or "", elapsed - waited)


@contextmanager
def human_wait(ctx: Any) -> Iterator[None]:
    """Ожидание человека: вычитается из хода. Закрывается и на отмене
    (стоп снимает чтение ответа ``CancelledError``), и на Ctrl+C."""
    clock = current_turn_clock(ctx)
    if clock is None:
        yield
        return
    clock.begin_wait()
    try:
        yield
    finally:
        clock.end_wait()
