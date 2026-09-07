from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

_logger = logging.getLogger(__name__)

DEFAULT_TODO_REMINDER_STEPS = 10


class TodoConfig(BaseModel):
    """Yaml section ``todo:`` — параметры чек-листа ``session_todo``."""

    model_config = ConfigDict(extra="forbid")

    # Каждые N шагов без вызова session_todo при незакрытых пунктах —
    # technical reminder. 0 — политика выключена.
    reminder_steps: int = DEFAULT_TODO_REMINDER_STEPS


def normalize_todo_config(cfg: TodoConfig) -> None:
    if cfg.reminder_steps < 0:
        _logger.warning(
            "todo.reminder_steps=%s must be >= 0; using 0 (disabled)",
            cfg.reminder_steps,
        )
        cfg.reminder_steps = 0


def todo_reminder_steps_from_config(config_dict: dict[str, Any] | None) -> int:
    params = (config_dict or {}).get("todo_params")
    if not isinstance(params, dict):
        return DEFAULT_TODO_REMINDER_STEPS
    try:
        steps = int(params.get("reminder_steps", DEFAULT_TODO_REMINDER_STEPS))
    except (TypeError, ValueError):
        return DEFAULT_TODO_REMINDER_STEPS
    return max(0, steps)
