"""Детерминированный fake AgentExecutor для snapshot-тестов TUI."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from gc_testkit.tui_fixtures.scenarios import SCENARIOS


class StubExecutor:
    """Записывает сценарий событий в data_*.jsonl с реалистичными паузами."""

    def __init__(
        self,
        *,
        threads_dir: str,
        system: str = "cli",
        scenario: str = "full_dialog",
        step_delay: float = 0.0,  # для snapshot-тестов делаем 0
    ) -> None:
        self.threads_dir = Path(threads_dir)
        self.system = system
        self.scenario = scenario
        self.step_delay = step_delay
        self.stopped = False

    def run(
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

        records = self._build_records(messages, scenario=self.scenario)
        with open(data_path, "a", encoding="utf-8") as f:
            for rec in records:
                if self.stopped:
                    return
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                if self.step_delay > 0:
                    time.sleep(self.step_delay)

        meta = {
            "thread_id": thread_id,
            "system_id": sys_,
            "updated_at": "2026-05-21T12:00:00+00:00",
            "messages_count": len(records),
        }
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )

    def stop_run(self) -> None:
        self.stopped = True

    def add_message(self, message: dict) -> None:
        pass  # для snapshot-тестов не используется

    def _build_records(
        self, user_messages: list, *, scenario: str
    ) -> list[dict]:
        return SCENARIOS[scenario](user_messages)
