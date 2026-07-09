"""Snapshot baseline перед text-мутацией FS-тулов (только TUI, cache-mode)."""
from __future__ import annotations

import logging
from typing import Any, Optional

from diff_tui.mode import resolve_mode
from diff_tui.store import DiffStore
from security.path_policy import resolve_tool_context

_logger = logging.getLogger(__name__)


def snapshot_before_text_mutation(tool_self: Any, path: str) -> Optional[str]:
    """Вызвать непосредственно перед ``_backend.write`` / ``.append``.

    - не TUI (CLI/server/swarm) → no-op
    - git-mode → no-op
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
        if resolve_mode(launch_root) == "git-mode":
            return None

        config_dict = getattr(context.ctx, "config_dict", None) or {}
        if not isinstance(config_dict, dict):
            config_dict = {}
        if not config_dict:
            raw = getattr(tool_self, "_config_dict", None)
            if isinstance(raw, dict):
                config_dict = raw

        diff_cfg = config_dict.get("diff_tui") or {}
        if not isinstance(diff_cfg, dict):
            diff_cfg = {}
        cache_max_orig = int(diff_cfg.get("cache_max_orig", 500))

        protected_block = config_dict.get("protected_paths") or {}
        protected_extra = (
            list(protected_block.get("extra") or [])
            if isinstance(protected_block, dict)
            else []
        )

        store = DiffStore(
            launch_root,
            extra_paths=list(context.write_guard.extra_paths),
            protected_extra=protected_extra,
            cache_max_orig=cache_max_orig,
        )
        return store.register_mutation(path)
    except Exception:
        _logger.exception("diff_tui snapshot failed path=%s", path)
        return None
