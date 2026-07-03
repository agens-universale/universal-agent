from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from instructions.layers import instruction_layer_specs

_PREAMBLE = (
    "Precedence: when instructions conflict, more specific instructions override "
    "more general ones; specificity increases down this list — project-personal "
    "overrides project-team, which overrides global."
)


@dataclass(frozen=True)
class LoadedInstructions:
    text: str
    loaded_ids: list[str]


def _read(path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def build_instructions(
    *,
    agent_md_path: str | None,
    cwd: str | None = None,
    overwrite: bool = False,
    logger: Any | None = None,
    verbose: bool = False,
) -> LoadedInstructions:
    specs = instruction_layer_specs(agent_md_path=agent_md_path, cwd=cwd)
    if overwrite:
        specs = [s for s in specs if s.scope == "project"]

    sections: list[str] = []
    loaded: list[str] = []
    for spec in specs:
        body = _read(spec.path)
        if body is None:
            # L0 is the caller-supplied override (agent_md_path) — unlike the
            # optional global/project layers, its absence/unreadability is
            # worth surfacing since the caller explicitly asked for it.
            if spec.id == "L0" and logger is not None and verbose:
                logger.warning(
                    "agent_md_read_failed",
                    extra={"event": "agent_md_read_failed", "path": str(spec.path)},
                )
            continue
        loaded.append(spec.id)
        sections.append(f"## {spec.title}\n{body.rstrip()}")

    if not sections:
        return LoadedInstructions(text="", loaded_ids=[])

    text = _PREAMBLE + "\n\n" + "\n\n".join(sections)
    return LoadedInstructions(text=text, loaded_ids=loaded)
