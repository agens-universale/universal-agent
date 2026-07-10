"""Snapshot baseline перед text-мутацией FS-тулов (только TUI).

cache-mode — всегда через DiffStore; git-mode — только ignored (cache-backend).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from diff_tui import git_ops
from diff_tui.mode import resolve_mode
from diff_tui.paths import canonical_path_key
from diff_tui.store import DiffStore
from security.path_policy import resolve_tool_context

_logger = logging.getLogger(__name__)


def _rel_for_check_ignore(path: str, launch_root: str, key: Optional[str]) -> str:
    if key is not None and not Path(key).is_absolute():
        return key
    try:
        return Path(path).resolve().relative_to(
            Path(launch_root).resolve()
        ).as_posix()
    except ValueError:
        return path


def snapshot_before_text_mutation(tool_self: Any, path: str) -> Optional[str]:
    """Вызвать непосредственно перед ``_backend.write`` / ``.append``.

    - не TUI (CLI/server/swarm) → no-op
    - git-mode + non-ignored → no-op (baseline = git index)
    - git-mode + ignored → ``DiffStore.register_mutation``
    - cache-mode → ``DiffStore.register_mutation``
    - сбой store не блокирует запись тула (soft-fail + log)

    Возвращает ``canonical_path_key`` или ``None``.
    """
    if not isinstance(path, str) or not path:
        return None

    try:
        context = resolve_tool_context(tool_self)
        # Diff-TUI — только обвязка TUI; CLI/server/swarm не пишут .gc/diffs.
        if not bool(getattr(context.ctx, "is_tui", False)):
            return None

        launch_root = context.launch_root
        config_dict = getattr(context.ctx, "config_dict", None) or {}
        if not isinstance(config_dict, dict):
            config_dict = {}
        if not config_dict:
            raw = getattr(tool_self, "_config_dict", None)
            if isinstance(raw, dict):
                config_dict = raw

        protected_block = config_dict.get("protected_paths") or {}
        protected_extra = (
            list(protected_block.get("extra") or [])
            if isinstance(protected_block, dict)
            else []
        )
        extra_paths = list(context.write_guard.extra_paths)

        key = canonical_path_key(
            path,
            launch_root,
            extra_paths=extra_paths,
            protected_extra=protected_extra,
        )
        if key is None:
            return None

        if resolve_mode(launch_root) == "git-mode":
            rel = _rel_for_check_ignore(path, launch_root, key)
            ignored = git_ops.check_ignore(launch_root, rel)
            # Только ignored → cache snapshot. fail-safe (None) — не пишем вслепую.
            if ignored is not True:
                return None

        diff_cfg = config_dict.get("diff_tui") or {}
        if not isinstance(diff_cfg, dict):
            diff_cfg = {}
        cache_max_orig = int(diff_cfg.get("cache_max_orig", 500))

        store = DiffStore(
            launch_root,
            extra_paths=extra_paths,
            protected_extra=protected_extra,
            cache_max_orig=cache_max_orig,
        )
        return store.register_mutation(path)
    except Exception:
        _logger.exception("diff_tui snapshot failed path=%s", path)
        return None
