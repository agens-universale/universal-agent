from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict

from gc_core.config_modules.editing import editable

_logger = logging.getLogger(__name__)

DEFAULT_TODO_REMINDER_STEPS = 10


class TodoConfig(BaseModel):
    """Yaml section ``todo:`` — параметры чек-листа ``session_todo``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Каждые N шагов без вызова session_todo при незакрытых пунктах —
    # technical reminder. 0 — политика выключена.
    reminder_steps: int = DEFAULT_TODO_REMINDER_STEPS


def normalize_todo_config(cfg: TodoConfig) -> TodoConfig:
    cfg = editable(cfg)
    if cfg.reminder_steps < 0:
        _logger.warning(
            "todo.reminder_steps=%s must be >= 0; using 0 (disabled)",
            cfg.reminder_steps,
        )
        cfg.reminder_steps = 0
    return cfg.result()
