from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from instructions.layers import instruction_layer_specs

_PREAMBLE = (
    "Precedence: when instructions conflict, more specific instructions override "
    "more general ones; specificity increases down this list."
)
_L0_TITLE = "Launch role instructions"
AGENTSMD_OVERRIDE_IGNORED_WARNING = (
    "agentsmd_override не применён: задан agent_md_path (путь главнее флага)"
)


@dataclass(frozen=True)
class LoadedInstructions:
    text: str
    loaded_ids: list[str]
    warnings: list[str] = field(default_factory=list)


def _read(path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Файл человека может лежать в любой кодировке; decode — подкласс
        # `ValueError`, а не `OSError`, и уходил мимо перехвата.
        return None


def _wrap(sections: list[str]) -> str:
    return _PREAMBLE + "\n\n" + "\n\n".join(sections)


def build_instructions(
    *,
    agent_md_path: str | None,
    is_tui: bool,
    agentsmd_override: bool = False,
    cwd: str | None = None,
    logger: Any | None = None,
    verbose: bool = False,
) -> LoadedInstructions:
    # 1. agent_md_path задан → ТОЛЬКО L0 (замена), в любом режиме. Путь > флаг.
    if agent_md_path:
        warnings: list[str] = []
        if is_tui and agentsmd_override:
            warnings.append(AGENTSMD_OVERRIDE_IGNORED_WARNING)
        body = _read(Path(agent_md_path))
        if body is None:
            # agent_md_path is the caller-supplied override — unlike the optional
            # global/project layers, its absence/unreadability is worth surfacing
            # (verbose only), since the caller explicitly asked for it.
            if logger is not None and verbose:
                logger.warning(
                    "agent_md_read_failed",
                    extra={"event": "agent_md_read_failed", "path": str(agent_md_path)},
                )
            return LoadedInstructions(text="", loaded_ids=[], warnings=warnings)
        text = _wrap([f"## {_L0_TITLE}\n{body.rstrip()}"])
        return LoadedInstructions(text=text, loaded_ids=["L0"], warnings=warnings)

    # 2. TUI без L0 → собрать L1–L3 (или только L3 при override).
    if is_tui:
        specs = instruction_layer_specs(cwd=cwd)
        if agentsmd_override:
            specs = [s for s in specs if s.id == "L3"]
        sections: list[str] = []
        loaded: list[str] = []
        for spec in specs:
            body = _read(spec.path)
            if body is None:
                continue
            loaded.append(spec.id)
            sections.append(f"## {spec.title}\n{body.rstrip()}")
        if not sections:
            return LoadedInstructions(text="", loaded_ids=[], warnings=[])
        return LoadedInstructions(text=_wrap(sections), loaded_ids=loaded, warnings=[])

    # 3. CLI/server/swarm без L0 → пусто.
    return LoadedInstructions(text="", loaded_ids=[], warnings=[])
