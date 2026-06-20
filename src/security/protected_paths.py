from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from config_modules.env_vars import gc_paths, project_gc_dir
from instructions.layers import instruction_file_paths


@dataclass(frozen=True)
class ProtectedSet:
    instructions: frozenset[Path]
    operational: frozenset[Path]
    skills_exceptions: frozenset[Path]


def protected_paths(*, cwd: str | None = None, extra: list[str] | None = None) -> ProtectedSet:
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
