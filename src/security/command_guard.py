"""Детерминированный tripwire для shell/python-команд: ловит ОЧЕВИДНЫЕ
обращения к protected-путям (rm -rf .gc и т.п.). Это НЕ гарантия — обфускация
не ловится; второй рубеж — LLM-судья (command_moderation), страховка — бэкап
памяти (Спека B §7)."""
from __future__ import annotations

import os
import re
import shlex

from security.protected_paths import classify_path, protected_paths

_TOKEN_RE = re.compile(r"[\s=;&|()<>]+")


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return [t for t in _TOKEN_RE.split(command) if t]


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
