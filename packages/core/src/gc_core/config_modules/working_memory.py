from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict

from gc_core.config_modules.editing import editable

_logger = logging.getLogger(__name__)

DEFAULT_MAX_CHARS_GOAL = 2_000
DEFAULT_MAX_CHARS_CONSTRAINTS = 4_000
DEFAULT_MAX_CHARS_STATE = 6_000
DEFAULT_MAX_CHARS_KNOWLEDGE = 8_000
DEFAULT_TOTAL_BUDGET_CHARS = 20_000


class WorkingMemoryConfig(BaseModel):
    """Yaml section ``working_memory:`` — char limits for ``session_memory`` registers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_chars_goal: int = DEFAULT_MAX_CHARS_GOAL
    max_chars_constraints: int = DEFAULT_MAX_CHARS_CONSTRAINTS
    max_chars_state: int = DEFAULT_MAX_CHARS_STATE
    max_chars_knowledge: int = DEFAULT_MAX_CHARS_KNOWLEDGE
    total_budget_chars: int = DEFAULT_TOTAL_BUDGET_CHARS


def normalize_working_memory_config(
    cfg: WorkingMemoryConfig,
) -> WorkingMemoryConfig:
    cfg = editable(cfg)
    if cfg.max_chars_goal <= 0:
        _logger.warning(
            "working_memory.max_chars_goal=%s must be > 0; using default %s",
            cfg.max_chars_goal,
            DEFAULT_MAX_CHARS_GOAL,
        )
        cfg.max_chars_goal = DEFAULT_MAX_CHARS_GOAL
    if cfg.max_chars_constraints <= 0:
        _logger.warning(
            "working_memory.max_chars_constraints=%s must be > 0; using default %s",
            cfg.max_chars_constraints,
            DEFAULT_MAX_CHARS_CONSTRAINTS,
        )
        cfg.max_chars_constraints = DEFAULT_MAX_CHARS_CONSTRAINTS
    if cfg.max_chars_state <= 0:
        _logger.warning(
            "working_memory.max_chars_state=%s must be > 0; using default %s",
            cfg.max_chars_state,
            DEFAULT_MAX_CHARS_STATE,
        )
        cfg.max_chars_state = DEFAULT_MAX_CHARS_STATE
    if cfg.max_chars_knowledge <= 0:
        _logger.warning(
            "working_memory.max_chars_knowledge=%s must be > 0; using default %s",
            cfg.max_chars_knowledge,
            DEFAULT_MAX_CHARS_KNOWLEDGE,
        )
        cfg.max_chars_knowledge = DEFAULT_MAX_CHARS_KNOWLEDGE
    if cfg.total_budget_chars <= 0:
        _logger.warning(
            "working_memory.total_budget_chars=%s must be > 0; using default %s",
            cfg.total_budget_chars,
            DEFAULT_TOTAL_BUDGET_CHARS,
        )
        cfg.total_budget_chars = DEFAULT_TOTAL_BUDGET_CHARS
    return cfg.result()


def working_memory_limits_from_params(params: dict | None) -> dict[str, int]:
    if not isinstance(params, dict):
        params = {}
    return {
        "max_chars_goal": int(
            params.get("max_chars_goal", DEFAULT_MAX_CHARS_GOAL)
        ),
        "max_chars_constraints": int(
            params.get("max_chars_constraints", DEFAULT_MAX_CHARS_CONSTRAINTS)
        ),
        "max_chars_state": int(
            params.get("max_chars_state", DEFAULT_MAX_CHARS_STATE)
        ),
        "max_chars_knowledge": int(
            params.get("max_chars_knowledge", DEFAULT_MAX_CHARS_KNOWLEDGE)
        ),
        "total_budget_chars": int(
            params.get("total_budget_chars", DEFAULT_TOTAL_BUDGET_CHARS)
        ),
    }
