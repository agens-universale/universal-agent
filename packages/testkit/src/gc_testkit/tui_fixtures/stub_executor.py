"""Детерминированный fake AgentExecutor для snapshot-тестов TUI."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from gc_core.execution.environment import utc_now
from gc_core.execution.message_keys import withdraw_from_queue
from gc_core.execution.turn_clock import Mono, TurnClock
from gc_core.persistence.turn_outcome import USER_STOP, TurnOutcome

from gc_testkit.thread_writes import turn_outcome_record
from gc_testkit.tui_fixtures.scenarios import (
    DEFAULT_OUTCOME,
    SCENARIO_OUTCOMES,
    SCENARIOS,
)


class StubExecutor:
    """Записывает сценарий событий в data_*.jsonl с реалистичными паузами.

    Ход закрывается, как у ядра, записью итога в поток ``turns``: у сценария — с закреплённым
    временем (`DEFAULT_OUTCOME` / `SCENARIO_OUTCOMES`), у остановленного —
    `stopped/user_stop` по часам хода (конец — момент стопа)."""

    def __init__(
        self,
        *,
        threads_dir: str,
        system: str = "cli",
        scenario: str = "full_dialog",
        step_delay: float = 0.0,  # для snapshot-тестов делаем 0
        mono: Mono = time.monotonic,
    ) -> None:
        self.threads_dir = Path(threads_dir)
        self.system = system
        self.scenario = scenario
        self.step_delay = step_delay
        self.stopped = False
        #: Очередь `add_message`: из неё снимает `withdraw_message`.
        self.queued: list[dict] = []
        self._mono = mono
        #: Часы текущего хода (как у ядра); до первого хода — None.
        self.clock: Optional[TurnClock] = None
        #: Состояние текущего прогона (как ``State`` ядра): только флаг
        #: ``produced_output`` — первая запись агента. До первого хода — None.
        self.state: Optional[SimpleNamespace] = None

    async def arun(
        self,
        messages: list,
        *,
        thread_id: str,
        start_new_thread: bool = False,
        system: Optional[str] = None,
        **_ignored,
    ) -> None:
        """Ход через раннер TUI: тот же сценарий, что и у `record`."""
        self.record(
            messages,
            thread_id=thread_id,
            start_new_thread=start_new_thread,
            system=system,
        )

    def record(
        self,
        messages: list,
        *,
        thread_id: str,
        start_new_thread: bool = False,
        system: Optional[str] = None,
        verbose: Optional[bool] = None,
    ) -> None:
        sys_ = system or self.system
        data_path = self.threads_dir / f"data_{sys_}_{thread_id}.jsonl"
        meta_path = self.threads_dir / f"meta_{sys_}_{thread_id}.json"

        self.clock = TurnClock.start(mono=self._mono, wall=utc_now)
        self.state = SimpleNamespace(produced_output=False)
        records = self._build_records(messages, scenario=self.scenario)
        written: list[dict] = []
        with open(data_path, "a", encoding="utf-8") as f:
            for rec in records:
                if self.stopped:
                    break
                if (rec.get("payload") or {}).get("role") != "user":
                    self.state.produced_output = True
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                written.append(rec)
                if self.step_delay > 0:
                    time.sleep(self.step_delay)
        self._record_outcome(sys_, thread_id, written)
        if self.stopped:
            return

        meta = {
            "thread_id": thread_id,
            "system_id": sys_,
            "updated_at": "2026-05-21T12:00:00+00:00",
            "messages_count": len(records),
        }
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )

    def get_state(self) -> Optional[SimpleNamespace]:
        return self.state

    def stop_run(self) -> None:
        self.stopped = True
        # Момент стопа — конец хода, как у ядра.
        if self.clock is not None:
            self.clock.close()

    def turn_effective_start(self) -> Optional[float]:
        """Старт хода, сдвинутый на ожидания человека; часов нет или они
        закрыты — None (как у ядра)."""
        if self.clock is None or self.clock.closed:
            return None
        return self.clock.effective_start()

    def add_message(self, message: dict) -> None:
        self.queued.append(message)

    def withdraw_message(self, msg_id: str) -> bool:
        return withdraw_from_queue(self.queued, msg_id)

    def _build_records(
        self, user_messages: list, *, scenario: str
    ) -> list[dict]:
        return SCENARIOS[scenario](user_messages)

    def _record_outcome(
        self, system: str, thread_id: str, written: list[dict]
    ) -> None:
        """Запись итога хода за последним сообщением; ход без сообщений
        и незакрытый ход сценария записи не получают."""
        if not written or self.clock is None:
            return
        self.clock.close()
        fields = self._outcome_fields()
        if fields is None:
            return
        outcome = TurnOutcome(opened_by=_opened_by(written), **fields)
        turn_outcome_record(
            str(self.threads_dir),
            system,
            thread_id,
            outcome,
            after=(written[-1].get("payload") or {}).get("id"),
        )

    def _outcome_fields(self) -> Optional[dict]:
        if not self.stopped:
            return SCENARIO_OUTCOMES.get(self.scenario, DEFAULT_OUTCOME)
        return {
            "status": "stopped",
            "reason": USER_STOP,
            "started_at": self.clock.started_at,
            "ended_at": self.clock.ended_at,
            "duration_sec": round(self.clock.elapsed(), 1),
            "error": None,
        }


def _opened_by(records: list[dict]) -> Optional[str]:
    """`id` первой реплики пользователя хода (как `opening_reply_id`)."""
    for rec in records:
        payload = rec.get("payload") or {}
        if payload.get("role") == "user":
            return payload.get("id")
    return None
