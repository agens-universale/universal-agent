from __future__ import annotations

import sys
from typing import Any

from constants.tool_names import ASK_HUMAN_TOOL_NAME
from security.path_policy import prompt_user_confirmation_choice

CONFIRM_ONCE = "once"
CONFIRM_SESSION = "session"
CONFIRM_DENY = "deny"
_VALID_CHOICES = {CONFIRM_ONCE, CONFIRM_SESSION, CONFIRM_DENY}


def _ask_human_enabled(ctx: Any) -> bool:
    cfg = getattr(ctx, "config_dict", None) or {}
    stop = cfg.get("stop_tools") or []
    return ASK_HUMAN_TOOL_NAME not in stop


def confirm_available(ctx: Any) -> bool:
    if getattr(ctx, "is_tui", False):
        return True
    if not _ask_human_enabled(ctx):
        return False
    stdin = getattr(sys, "stdin", None)
    return bool(stdin and stdin.isatty())


def _ask_choice(ctx: Any, *, title: str, question: str, fields: list) -> str:
    if not confirm_available(ctx):
        return CONFIRM_DENY
    if getattr(ctx, "is_tui", False) and getattr(ctx, "tui_confirm", None):
        choice = ctx.tui_confirm(title=title, question=question, fields=fields)
        return choice if choice in _VALID_CHOICES else CONFIRM_DENY
    return prompt_user_confirmation_choice(title=title, fields=fields, question=question)


def request_confirm(
    *, title: str, question: str, fields: list[tuple[str, object]], ctx: Any,
    decision_key: str | None = None,
) -> bool:
    store = getattr(ctx, "permission_store", None)
    if store is not None and decision_key is not None:
        cached = store.decision_for(decision_key)
        if cached is not None:
            return cached
    choice = _ask_choice(ctx, title=title, question=question, fields=fields)
    allow = choice in (CONFIRM_ONCE, CONFIRM_SESSION)
    if store is not None and decision_key is not None:
        store.record(decision_key, allow, session=(choice == CONFIRM_SESSION))
    return allow
