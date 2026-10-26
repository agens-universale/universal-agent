"""Владение ключами thread meta: ``tui`` (TUI) vs ``runtime`` (ядро/persist).

См. docs_md/tui/meta-ownership/SPEC.md.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, MutableMapping

RUNTIME_FLAG_NAMES = frozenset(
    {"summarizing", "finalizing", "finish_gate_checking"}
)


def resolve_runtime_flag_name(flag: str) -> str:
    """Каноническое имя флага в ``meta.runtime``."""
    if flag not in RUNTIME_FLAG_NAMES:
        raise ValueError(f"unknown runtime flag: {flag!r}")
    return flag


def runtime_section(meta: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Снимок ``meta.runtime``."""
    if not meta:
        return {}
    rt = meta.get("runtime") or {}
    return dict(rt) if isinstance(rt, dict) else {}


def merge_runtime_into_meta(
    meta: MutableMapping[str, Any], patch: Mapping[str, Any]
) -> None:
    """Слить patch в ``meta['runtime']`` in-place."""
    runtime = dict(meta.get("runtime") or {})
    runtime.update(dict(patch))
    meta["runtime"] = runtime


def clear_run_flags(
    meta: MutableMapping[str, Any], *, stale_summarizing: bool = False
) -> None:
    """Apply the startup reset policy to freshly read metadata."""
    runtime = dict(meta.get("runtime") or {})
    if stale_summarizing or runtime.get("summarizing"):
        runtime.pop("summarizing", None)
    runtime.pop("finalizing", None)
    runtime.pop("finish_gate_checking", None)
    if runtime:
        meta["runtime"] = runtime
    else:
        meta.pop("runtime", None)
