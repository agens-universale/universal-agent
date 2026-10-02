from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gc_core.config_modules.env_vars import gc_paths, project_gc_dir
from gc_core.instructions.layers import instruction_file_paths
from gc_core.security.path_policy import (
    _is_flat_source,
    _setting_or,
    is_within_root,
)
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


def protection_enabled(config_dict: Any) -> bool:
    """Включена ли floor-защита системных путей (по умолчанию — да).

    Выключает её только явный ``security.protected_paths.enabled: false`` в
    плоском словаре (так его передаёт temporal-воркер; тесты). Типизированные
    настройки сюда прежде не доходили — ``flatten_app_config`` секцию не
    проецирует, — поэтому для них защита включена всегда, как и было.
    """
    if not _is_flat_source(config_dict):
        return True
    settings = settings_of(config_dict)
    return bool(
        _setting_or(settings, True, "security.protected_paths.enabled", True)
    )
