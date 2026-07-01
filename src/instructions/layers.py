from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from config_modules.env_vars import effective_config_dir, project_gc_dir

_BRAND = "AGENTS.md"


@dataclass(frozen=True)
class LayerSpec:
    id: str
    title: str  # английский заголовок-маркер для инжект-блока
    path: Path
    scope: str  # "shipped" | "global" | "project"


def _global_instruction_path() -> Path:
    return effective_config_dir() / _BRAND


def instruction_file_paths(*, cwd: str | None = None) -> dict[str, Path]:
    """Пути файлов-инструкций L1/L2/L3 (без L0). Единый источник для loader и protected_paths()."""
    proj = Path(cwd) if cwd else Path(os.getcwd())
    return {
        "L1": _global_instruction_path(),
        "L2": proj / "AGENTS.md",
        "L3": project_gc_dir(str(proj)) / _BRAND,
    }


def instruction_layer_specs(
    *, agent_md_path: str | None, cwd: str | None = None
) -> list[LayerSpec]:
    files = instruction_file_paths(cwd=cwd)
    l0 = Path(agent_md_path) if agent_md_path else None
    specs: list[LayerSpec] = []
    if l0 is not None:
        specs.append(LayerSpec("L0", "Base instructions (shipped defaults)", l0, "shipped"))
    specs.append(LayerSpec("L1", f"Global instructions ({files['L1']})", files["L1"], "global"))
    specs.append(LayerSpec("L2", "Project instructions (AGENTS.md)", files["L2"], "project"))
    specs.append(LayerSpec("L3", "Project instructions (.gc/AGENTS.md)", files["L3"], "project"))
    return specs
