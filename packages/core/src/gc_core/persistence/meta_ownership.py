"""Владение ключами thread meta: ``tui`` (TUI) vs ``runtime`` (ядро/persist).

См. docs_md/tui/meta-ownership/SPEC.md.
"""

from __future__ import annotations

RUNTIME_FLAG_NAMES = frozenset(
    {"summarizing", "finalizing", "finish_gate_checking"}
)


def resolve_runtime_flag_name(flag: str) -> str:
    """Каноническое имя флага в ``meta.runtime``."""
    if flag not in RUNTIME_FLAG_NAMES:
        raise ValueError(f"unknown runtime flag: {flag!r}")
    return flag
