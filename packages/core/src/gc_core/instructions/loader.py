from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from gc_core.agent_store import (
    AgentStore,
    ProjectKey,
    aread_or_none,
    decode_text,
    project_key_for,
)
from gc_core.instructions.layers import instruction_layer_specs

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


async def _aread_layer(
    store: AgentStore, spec, project: ProjectKey
) -> str | None:
    """L1 — ресурс агента; L2 и L3 — ресурсы проекта рана."""
    if spec.id == "L1":
        return await aread_or_none(store, "instructions", "L1")
    return await aread_or_none(store, "instructions", spec.id, project=project)


async def _aread_l0(store: AgentStore) -> tuple[bool, str | None]:
    """(задана ли роль запуска, её текст). Задана, но не читается —
    ``(True, None)``: о таком отказе предупреждаем."""
    try:
        data = await store.read("instructions", "L0")
    except (OSError, UnicodeDecodeError):
        return True, None
    if data is None:
        return False, None
    try:
        return True, decode_text(data)
    except UnicodeDecodeError:
        return True, None


def _wrap(sections: list[str]) -> str:
    return _PREAMBLE + "\n\n" + "\n\n".join(sections)


def _load_l0(
    label: str,
    *,
    is_tui: bool,
    agentsmd_override: bool,
    logger: Any | None,
    verbose: bool,
    body: str | None,
) -> LoadedInstructions:
    warnings: list[str] = []
    if is_tui and agentsmd_override:
        warnings.append(AGENTSMD_OVERRIDE_IGNORED_WARNING)
    if body is None:
        # agent_md_path is the caller-supplied override — unlike the optional
        # global/project layers, its absence/unreadability is worth surfacing
        # (verbose only), since the caller explicitly asked for it.
        if logger is not None and verbose:
            logger.warning(
                "agent_md_read_failed",
                extra={
                    "event": "agent_md_read_failed",
                    "path": label,
                },
            )
        return LoadedInstructions(text="", loaded_ids=[], warnings=warnings)
    text = _wrap([f"## {_L0_TITLE}\n{body.rstrip()}"])
    return LoadedInstructions(text=text, loaded_ids=["L0"], warnings=warnings)


async def _aload_layers(
    specs, store: AgentStore, project: ProjectKey
) -> LoadedInstructions:
    sections: list[str] = []
    loaded: list[str] = []
    for spec in specs:
        body = await _aread_layer(store, spec, project)
        if body is None:
            continue
        loaded.append(spec.id)
        sections.append(f"## {spec.title}\n{body.rstrip()}")
    if not sections:
        return LoadedInstructions(text="", loaded_ids=[], warnings=[])
    return LoadedInstructions(
        text=_wrap(sections), loaded_ids=loaded, warnings=[]
    )


async def abuild_instructions(
    *,
    is_tui: bool,
    store: AgentStore,
    agentsmd_override: bool = False,
    cwd: str | None = None,
    project: ProjectKey | None = None,
    logger: Any | None = None,
    verbose: bool = False,
) -> LoadedInstructions:
    """Инструкции рана. ``project`` — проект для L2 и L3 (у файлового
    хранилища — каталог); не задан — каталог ``cwd`` (текущий)."""
    # 1. Роль запуска (L0) задана → ТОЛЬКО L0 (замена), в любом режиме. Путь >
    # флаг. У файлового хранилища она задана путём ``paths.agent_md_path``;
    # своё хранилище может отдать её и без пути.
    configured, l0 = await _aread_l0(store)
    if configured:
        return _load_l0(
            store.label("instructions", "L0"),
            is_tui=is_tui,
            agentsmd_override=agentsmd_override,
            logger=logger,
            verbose=verbose,
            body=l0,
        )

    # 2. TUI без L0 → собрать L1–L3 (или только L3 при override).
    if is_tui:
        specs = instruction_layer_specs(cwd=cwd)
        if agentsmd_override:
            specs = [s for s in specs if s.id == "L3"]
        if project is None:
            project = project_key_for(cwd or os.getcwd())
        return await _aload_layers(specs, store, project)

    # 3. CLI/server/swarm без L0 → пусто.
    return LoadedInstructions(text="", loaded_ids=[], warnings=[])
