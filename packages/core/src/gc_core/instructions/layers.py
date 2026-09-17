from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from gc_core.config_modules.env_vars import effective_config_dir, project_gc_dir

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
        "L2": project_gc_dir(str(proj)) / _BRAND,
        "L3": proj / "AGENTS.md",
    }


def instruction_layer_specs(*, cwd: str | None = None) -> list[LayerSpec]:
    files = instruction_file_paths(cwd=cwd)
    return [
        LayerSpec("L1", f"Global instructions ({files['L1']})", files["L1"], "global"),
        LayerSpec("L2", "Project instructions (.gc/AGENTS.md)", files["L2"], "project"),
        LayerSpec("L3", "Project instructions (AGENTS.md)", files["L3"], "project"),
    ]
