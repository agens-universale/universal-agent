from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gc_core.config_modules.env_vars import gc_paths, project_gc_dir
from gc_core.instructions.layers import instruction_file_paths
from gc_core.security.path_policy import is_within_root
from gc_core.settings import settings_of


@dataclass(frozen=True)
class ProtectedSet:
    instructions: frozenset[Path]
    operational: frozenset[Path]
    skills_exceptions: frozenset[Path]


def protected_paths(
    *, cwd: str | None = None, extra: list[str] | None = None
) -> ProtectedSet:
    proj = Path(cwd) if cwd else Path(os.getcwd())
    paths = gc_paths()
    instr = set(instruction_file_paths(cwd=str(proj)).values())

    operational = {project_gc_dir(str(proj))}
    for key in ("config", "state", "cache", "data"):
        operational.add(paths[key])
    for raw in extra or []:
        operational.add(Path(os.path.abspath(raw)))

    skills = {project_gc_dir(str(proj)) / "skills", paths["data"] / "skills"}

    return ProtectedSet(
        instructions=frozenset(instr),
        operational=frozenset(operational),
        skills_exceptions=frozenset(skills),
    )


def classify_path(path, ps: ProtectedSet) -> str:
    p = os.path.abspath(os.fspath(path))
    ci = os.name == "nt"
    if any(
        is_within_root(p, r, case_insensitive=ci) for r in ps.skills_exceptions
    ):
        return "skills"
    if any(is_within_root(p, r, case_insensitive=ci) for r in ps.instructions):
        return "instructions"
    if any(is_within_root(p, r, case_insensitive=ci) for r in ps.operational):
        return "operational"
    return "none"


def protected_decision(path, *, cwd=None, extra=None) -> str:
    cls = classify_path(path, protected_paths(cwd=cwd, extra=extra))
    if cls == "instructions":
        return "ask"
    if cls == "operational":
        return "deny"
    return "allow"


def protected_extra_paths(source: Any) -> list[str]:
    """Дополнительные защищённые пути из ``security.protected_paths.extra``.

    Это ЗАПРЕТНЫЕ пути («нельзя читать/писать агенту»), а не разрешённые:
    ``write_guard.extra_paths`` — их противоположность (дополнительные каталоги
    для записи), и в набор защищённых им не место. ``~`` раскрывается здесь —
    ``protected_paths`` берёт только ``abspath``. ``source`` — то, что
    принимает ``settings_of``.
    """
    return [
        os.path.expanduser(raw)
        for raw in settings_of(source).security.protected_paths.extra
        if str(raw).strip()
    ]


def protection_enabled(settings: Any) -> bool:
    """Включена ли floor-защита системных путей — всегда да.

    ``security.protected_paths.enabled`` сейчас не действует (см.
    docs/configuration.md): до гардов этот флаг не доходил никогда — плоский
    вид настроек секцию не проецировал, — и значение ``false`` защиту не
    снимает. ``settings`` (то, что принимает ``settings_of``) оставлен ради
    сигнатуры: включать флаг — отдельное решение, а не побочный эффект
    перехода на типизированные настройки.
    """
    del settings
    return True
