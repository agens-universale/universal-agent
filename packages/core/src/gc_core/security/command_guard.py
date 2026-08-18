"""Детерминированный tripwire для shell/python-команд: ловит ОЧЕВИДНЫЕ
обращения к protected-путям (rm -rf .gc и т.п.). Это НЕ гарантия — обфускация
не ловится; второй рубеж — LLM-судья (command_moderation), страховка — бэкап
памяти (Спека B §7)."""
from __future__ import annotations

import functools
import inspect
import json
import os
import re
import shlex
from typing import Any, Callable

from gc_core.security.command_moderation import NO_WORKAROUND_NOTICE, effective_command_cwd
from gc_core.security.path_policy import resolve_tool_context, _extract_config_dict
from gc_core.security.protected_paths import classify_path, protected_paths, protection_enabled

_TOKEN_RE = re.compile(r"[\s=;&|()<>]+")


#: Разделители команд в shell. Резать по ним НАДО до токенизации: shlex их
#: разделителями не считает, и `rm -rf .gc; echo x` даёт токен `.gc;` — с
#: точкой с запятой внутри. Проверка пути такой токен не узнаёт, и tripwire
#: пропускал команду, которую с `&&` блокировал (найдено 18.08.2026: агент
#: удалил .gc, приписав `; echo "exit=$?"`).
_SHELL_SEPARATORS = re.compile(r"[;&|\n\r]+")


def _tokens(command: str) -> list[str]:
    # posix=True on POSIX (backslash as escape char); posix=False on Windows
    # (backslash is path separator, not escape — shlex would strip it otherwise).
    posix = os.name != "nt"
    out: list[str] = []
    for segment in _SHELL_SEPARATORS.split(command):
        if not segment.strip():
            continue
        try:
            out.extend(shlex.split(segment, posix=posix))
        except ValueError:
            out.extend(t for t in _TOKEN_RE.split(segment) if t)
    return out


def scan_command_for_protected(command: str, *, cwd: str, extra=None) -> str:
    ps = protected_paths(cwd=cwd, extra=extra)
    worst = "allow"
    rank = {"allow": 0, "ask": 1, "deny": 2}
    for tok in _tokens(command):
        cand = tok if os.path.isabs(tok) else os.path.join(cwd, tok)
        cls = classify_path(cand, ps)
        decision = {"skills": "allow", "none": "allow",
                    "instructions": "ask", "operational": "deny"}[cls]
        if rank[decision] > rank[worst]:
            worst = decision
    return worst


def enforce_command_guard(
    payload_arg: str = "command",
    tool_name: str = "",
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Deterministic pre-check decorator: runs BEFORE the LLM judge.

    deny  → returns a blocked result immediately (no subprocess spawned).
    ask   → prompts via confirm_port; if not confirmed → blocked.
    allow → falls through to the decorated function unchanged.

    Activated only when protection_enabled(config_dict) is True.
    """
    def decorator(func: Callable[..., str]) -> Callable[..., str]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            self_obj = args[0] if args else None
            ctx_obj = kwargs.get("ctx") or getattr(self_obj, "_ctx", None)
            cfg = _extract_config_dict(self_obj, ctx_obj, kwargs)

            if not protection_enabled(cfg):
                return func(*args, **kwargs)

            # Extract the payload (command string or code string).
            try:
                bound = inspect.signature(func).bind_partial(*args, **kwargs)
                payload = bound.arguments.get(payload_arg)
                if not isinstance(payload, str):
                    return func(*args, **kwargs)
            except Exception:
                return func(*args, **kwargs)

            # For the security scan we need the project root (launch_root) so that
            # project_gc_dir resolves the correct .gc directory.  Relative path
            # tokens in commands are also resolved against launch_root — the
            # project root is the canonical base for policy checks.
            context = resolve_tool_context(*args, **kwargs)
            effective_tool_name = tool_name or getattr(func, "__name__", "")
            launch_root = context.launch_root
            if not launch_root:
                # Fallback: use effective command cwd
                launch_root = effective_command_cwd(effective_tool_name, kwargs, context)

            extra = list(context.write_guard.extra_paths) or None
            decision = scan_command_for_protected(payload, cwd=launch_root, extra=extra)

            if decision == "allow":
                return func(*args, **kwargs)

            if decision == "ask":
                from gc_core.security.confirm_port import request_confirm
                confirmed = request_confirm(
                    title="Защищённый путь",
                    question="Команда затрагивает защищённый путь. Выполнить?",
                    fields=[("command", payload)],
                    ctx=ctx_obj,
                    decision_key=f"command:{payload}",
                )
                if confirmed:
                    return func(*args, **kwargs)
                # ask not confirmed → instructive blocked result
                err = {
                    "type": "protected_path_denied",
                    "message": "User declined the command. " + NO_WORKAROUND_NOTICE,
                    "command_preview": payload[:200],
                }
                blocked_method = getattr(self_obj, "_command_moderation_blocked_result", None)
                if callable(blocked_method):
                    return blocked_method(err, *args[1:], **kwargs)
                return json.dumps(
                    {"tool": effective_tool_name, "ok": False, "error": err},
                    ensure_ascii=False,
                )

            # deny → static blocked result
            err = {
                "type": "protected_path_denied",
                "message": "Command targets a protected path (.gc / config).",
                # Тот же класс обхода, что и у судьи: заблокированную команду
                # модель переписывает в эквивалент. Запрет — одной формулировкой
                # с модерацией (NO_WORKAROUND_NOTICE).
                "guidance": NO_WORKAROUND_NOTICE,
                "command_preview": payload[:200],
            }
            # Use tool's own blocked-result method if available (mirrors moderation pattern).
            blocked_method = getattr(self_obj, "_command_moderation_blocked_result", None)
            if callable(blocked_method):
                return blocked_method(err, *args[1:], **kwargs)
            return json.dumps(
                {"tool": effective_tool_name, "ok": False, "error": err},
                ensure_ascii=False,
            )

        return wrapper

    return decorator
