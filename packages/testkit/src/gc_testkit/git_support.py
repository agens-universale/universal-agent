"""Git в тестах: помощники, которых нет у прод-кода."""

from __future__ import annotations

from gc_core.agent_edits.git_ops import run_git


def rev_parse_head(launch_root: str) -> str | None:
    """``git rev-parse HEAD`` → полный oid коммита; ``None`` — нет коммитов/не git.

    Решает КОД возврата, не непустой вывод: на отказе ``rev-parse`` иногда всё
    равно печатает нерезолвленный ``HEAD`` в stdout.
    """
    code, out = run_git(launch_root, ["rev-parse", "HEAD"])
    if code != 0:
        return None
    text = out.decode("utf-8", errors="replace").strip()
    return text or None
