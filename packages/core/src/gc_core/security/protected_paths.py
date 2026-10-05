from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from gc_core.config_modules.env_vars import gc_paths, project_gc_dir
from gc_core.instructions.layers import instruction_file_paths
from gc_core.security.path_policy import is_within_root


@dataclass(frozen=True)
class ProtectedSet:
    instructions: frozenset[Path]
    operational: frozenset[Path]
    skills_exceptions: frozenset[Path]
    #: Рабочая область рана, лежащая ВНУТРИ служебного корня gc (sandbox
    #: ``workdir: auto`` → ``<state>/sandboxes/<run_id>``). Сама она не
    #: служебная — иначе каждая команда в ней ловила бы отказ; служебными
    #: остаются её ``.gc`` и ``extra`` (``operational_hard``).
    workspace_exception: Path | None = None
    operational_hard: frozenset[Path] = frozenset()


def protected_paths(
    *, cwd: str | None = None, extra: list[str] | None = None
) -> ProtectedSet:
    proj = Path(cwd) if cwd else Path(os.getcwd())
    paths = gc_paths()
    instr = set(instruction_file_paths(cwd=str(proj)).values())

    hard = {project_gc_dir(str(proj))}
    for raw in extra or []:
        hard.add(Path(os.path.abspath(raw)))
    gc_roots = {paths[key] for key in ("config", "state", "cache", "data")}
    ci = os.name == "nt"
    # Только каталог рана песочницы: cwd=~/.config/gc не снимает защиту с config.yml.
    sandboxes = paths["state"] / "sandboxes"
    workspace = (
        proj
        if is_within_root(str(proj), str(sandboxes), case_insensitive=ci)
        and proj != sandboxes
        else None
    )

    # skills_runtime — копия слоёв скиллов в среде исполнения remote-бэкенда
    # (tools/skills/provision.py): для модели это те же скиллы, что и
    # `.gc/skills`, иначе чтение скрипта скилла ловит protected_path_denied.
    skills = {
        project_gc_dir(str(proj)) / "skills",
        project_gc_dir(str(proj)) / "skills_runtime",
        paths["data"] / "skills",
    }

    return ProtectedSet(
        instructions=frozenset(instr),
        operational=frozenset(hard | gc_roots),
        skills_exceptions=frozenset(skills),
        workspace_exception=workspace,
        operational_hard=frozenset(hard),
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
    if any(
        is_within_root(p, r, case_insensitive=ci) for r in ps.operational_hard
    ):
        return "operational"
    if ps.workspace_exception is not None and is_within_root(
        p, ps.workspace_exception, case_insensitive=ci
    ):
        return "none"
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


def protection_enabled(config_dict: dict) -> bool:
    block = (config_dict or {}).get("protected_paths") or {}
    return bool(block.get("enabled", True))
