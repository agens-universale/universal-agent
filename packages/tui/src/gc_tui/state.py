"""TuiState — UI-состояние TUI-приложения.

Хранит активную задачу (`active_task_id`), выбранный скилл (`pending_skill_id`)
и состояние ask_human (`ask_human_state` — читает header для бейджа `[asking]`).
`dismissed_cut_commands` — урезанные потолком команды, строку о которых на
экране изменений скрыли клавишей `x`: (system_id, thread_id, ход, число путей),
до выхода из приложения.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Set, Tuple


@dataclass
class AskHumanState:
    call_id: str
    question: str


@dataclass
class TuiState:
    active_task_id: Optional[str] = None
    pending_skill_id: Optional[str] = None
    ask_human_state: Optional[AskHumanState] = None
    dismissed_cut_commands: Set[Tuple[str, str, str, int]] = field(default_factory=set)
