"""TuiState — UI-состояние TUI-приложения.

Хранит активную задачу (`active_task_id`), выбранный скилл (`pending_skill_id`)
и состояние ask_human (`ask_human_state` — читает header для бейджа `[asking]`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class AskHumanState:
    call_id: str
    question: str


@dataclass
class TuiState:
    active_task_id: Optional[str] = None
    pending_skill_id: Optional[str] = None
    ask_human_state: Optional[AskHumanState] = None
