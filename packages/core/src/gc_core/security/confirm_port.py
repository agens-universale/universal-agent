from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

from gc_core.constants.tool_names import ASK_HUMAN_TOOL_NAME

CONFIRM_ALLOW_REMEMBER = "allow_remember"   # Y — exec + session True + few-shot allowed
CONFIRM_ALLOW_ONCE = "allow_once"       # O — exec + run only
CONFIRM_DENY_REMEMBER = "deny_remember"  # N — block + session False + few-shot denied
CONFIRM_DENY_ONCE = "deny_once"        # T — block + run only
_VALID_CHOICES = {
    CONFIRM_ALLOW_REMEMBER,
    CONFIRM_ALLOW_ONCE,
    CONFIRM_DENY_REMEMBER,
    CONFIRM_DENY_ONCE,
}

# Canon key sets — same physical key on QWERTY and ЙЦУКЕН (TUI event.key + CLI input).
# Н ≠ Latin N; Т on ЙЦУКЕН is physical N, not Latin T.
CONFIRM_KEY_Y = frozenset({"y", "Y", "н", "Н"})  # allow_remember
CONFIRM_KEY_O = frozenset({"o", "O", "щ", "Щ"})  # allow_once
CONFIRM_KEY_N = frozenset({"n", "N", "т", "Т"})  # deny_remember
CONFIRM_KEY_T = frozenset({"t", "T", "е", "Е"})  # deny_once

# Human-facing one-liner (CLI prompt / docs); mirrors banner Y/O/N/T labels.
CONFIRM_CHOICE_HINT = (
    "[Y/Н] и подобные в сессии  [O/Щ] разово  "
    "[N/Т] запрет+ в сессии  [T/Е] запрет разово"
)

# Backward-compat aliases (optional one release): old tui_confirm returns
# once|session|deny — маппим в новые 4-choice ids. CLI input() aliases removed.
_ALIAS_MAP = {
    "once": CONFIRM_ALLOW_ONCE,
    "session": CONFIRM_ALLOW_REMEMBER,
    "deny": CONFIRM_DENY_REMEMBER,
}

# Old constants kept as aliases for external callers (one release).
CONFIRM_ONCE = CONFIRM_ALLOW_ONCE
CONFIRM_SESSION = CONFIRM_ALLOW_REMEMBER
CONFIRM_DENY = CONFIRM_DENY_REMEMBER


def normalize_confirm_key(answer: str | None) -> str:
    """Map a single key / CLI answer to a 4-way choice id (TUI + CLI).

    Accepts Latin/Cyrillic same-key glyphs in either case. Multi-char words
    (yes/no/once) are not accepted — same as TUI single-key handlers.
    Unknown / empty → deny_remember (explicit-safe).
    """
    if answer is None:
        return CONFIRM_DENY_REMEMBER
    token = str(answer).strip()
    if not token:
        return CONFIRM_DENY_REMEMBER
    # Single key only (TUI parity). Full-width / accidental paste → deny_remember.
    if len(token) != 1:
        return CONFIRM_DENY_REMEMBER
    if token in CONFIRM_KEY_Y:
        return CONFIRM_ALLOW_REMEMBER
    if token in CONFIRM_KEY_O:
        return CONFIRM_ALLOW_ONCE
    if token in CONFIRM_KEY_N:
        return CONFIRM_DENY_REMEMBER
    if token in CONFIRM_KEY_T:
        return CONFIRM_DENY_ONCE
    return CONFIRM_DENY_REMEMBER


def coerce_confirm_choice(choice: Any) -> str:
    """Normalize UI/port return value to a valid 4-way id."""
    if choice in _VALID_CHOICES:
        return choice  # type: ignore[return-value]
    if isinstance(choice, str) and choice in _ALIAS_MAP:
        return _ALIAS_MAP[choice]
    return CONFIRM_DENY_REMEMBER


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


def _via_name(ctx: Any) -> str | None:
    name = getattr(ctx, "subagent_tool_name", None)
    return name if isinstance(name, str) and name.strip() else None


def _ask_choice(ctx: Any, *, title: str, question: str, fields: list) -> str:
    # Confirm недоступен / AFK-like: block without teaching (deny_once).
    # Explicit invalid UI choice → deny_remember (fail-closed + few-shot).
    if not confirm_available(ctx):
        return CONFIRM_DENY_ONCE

    # Lazy: security → tools на module load ломает import graph (paths/diff_tui).
    from gc_core.tools.subagents.progress import emit_progress
    # Nested: path_policy → confirm_port cycle if imported at module top.
    from gc_core.security.path_policy import prompt_user_confirmation_choice

    via = _via_name(ctx)
    # confirm-порт не знает steps child — для progress достаточно нулей
    emit_state = SimpleNamespace(steps=0, max_steps=0)
    emit_progress(
        ctx,
        emit_state,
        "confirm",
        prose=question,
        tool_name=via,
        args_summary=title,
    )
    try:
        if getattr(ctx, "is_tui", False):
            tui_confirm = getattr(ctx, "tui_confirm", None)
            if not callable(tui_confirm):
                # Нет рабочего present → deny_once (не учить few-shot), не CLI input() за Textual.
                return CONFIRM_DENY_ONCE
            try:
                choice = tui_confirm(
                    title=title, question=question, fields=fields, via=via,
                )
            except TypeError:
                choice = tui_confirm(title=title, question=question, fields=fields)
            result = coerce_confirm_choice(choice)
        else:
            # Non-TUI fallback (headless CLI / scripts). Product path is TUI;
            # mode=ask is not the intended CLI UX (allow|deny policy instead),
            # but if confirm is reached, key matrix matches TUI.
            result = coerce_confirm_choice(
                prompt_user_confirmation_choice(
                    title=title, fields=fields, question=question,
                )
            )
    finally:
        emit_progress(
            ctx,
            emit_state,
            "confirm_done",
            tool_name=via,
            args_summary=title,
        )
    return result


def request_confirm(
    *, title: str, question: str, fields: list[tuple[str, object]], ctx: Any,
    decision_key: str | None = None,
) -> bool:
    # Lazy: security → tools на module load ломает import graph.
    from gc_core.tools.connect_mcp.abandonable import current_call_abandoned

    store = getattr(ctx, "permission_store", None)
    if store is not None and decision_key is not None:
        cached = store.decision_for(decision_key)
        if cached is not None:
            return cached
    if current_call_abandoned():
        return False
    choice = _ask_choice(ctx, title=title, question=question, fields=fields)
    if current_call_abandoned():
        # Вопрос бросили (Ctrl+C), пока человек думал: ответ, если он и пришёл,
        # набран уже для ДРУГОГО вопроса. Записать его — значит выдать
        # разрешение на путь, в котором человек только что отказал.
        return False
    allow = choice in (CONFIRM_ALLOW_REMEMBER, CONFIRM_ALLOW_ONCE)
    if store is not None and decision_key is not None:
        # 2×2 record matrix: (allow, session) per choice.
        #   Y=allow_remember → (True, True)
        #   O=allow_once     → (True, False)
        #   N=deny_remember  → (False, True)
        #   T=deny_once      → (False, False)
        session = choice in (CONFIRM_ALLOW_REMEMBER, CONFIRM_DENY_REMEMBER)
        store.record(decision_key, allow, session=session)
    return allow
