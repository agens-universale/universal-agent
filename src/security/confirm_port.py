from __future__ import annotations

import sys
from typing import Any

from security.path_policy import prompt_user_confirmation


def _ask_human_enabled(ctx: Any) -> bool:
    cfg = getattr(ctx, "config_dict", None) or {}
    stop = cfg.get("stop_tools") or []
    return "ask_human" not in stop


def confirm_available(ctx: Any) -> bool:
    if getattr(ctx, "is_tui", False):
        return True
    if not _ask_human_enabled(ctx):
        return False
    stdin = getattr(sys, "stdin", None)
    return bool(stdin and stdin.isatty())


def request_confirm(*, title: str, question: str, fields: list[tuple[str, object]], ctx: Any) -> bool:
    if not confirm_available(ctx):
        return False
    if getattr(ctx, "is_tui", False) and getattr(ctx, "tui_confirm", None):
        return bool(ctx.tui_confirm(title=title, question=question, fields=fields))
    return prompt_user_confirmation(title=title, fields=fields, question=question)
