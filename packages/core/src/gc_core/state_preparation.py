"""Local preparation of external values consumed by the existing State.

Only the request and result cross an execution boundary. A caller resolves
workspace, configuration and logging on the side performing preparation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from gc_core.instructions.loader import LoadedInstructions, build_instructions
from gc_core.project_memory import attach_status_to_prompt_vars


@dataclass(frozen=True)
class StatePreparationRequest:
    agent_md_path: str | None = None
    is_tui: bool = False
    agentsmd_override: bool = False
    verbose: bool = False


@dataclass(frozen=True)
class PreparedState:
    state_id: str
    project_data: dict[str, Any]
    instructions_text: str
    instruction_warnings: list[str] = field(default_factory=list)
    long_memory_status: str | None = None


StatePreparer = Callable[[StatePreparationRequest], Awaitable[PreparedState]]


def load_state_metadata(
    request: StatePreparationRequest,
    *,
    logger: Any = None,
    metadata_config: Any = None,
) -> tuple[str | None, LoadedInstructions]:
    """Read instruction layers and project-memory status in the local runtime.

    ``metadata_config`` — всё, что принимает ``settings_of`` (контекст, настройки).
    """
    variables: dict[str, Any] = {}
    attach_status_to_prompt_vars(variables, settings=metadata_config)
    instructions = build_instructions(
        agent_md_path=request.agent_md_path,
        is_tui=request.is_tui,
        agentsmd_override=request.agentsmd_override,
        logger=logger,
        verbose=request.verbose,
    )
    return variables.get("longMemoryStatus"), instructions


async def prepare_state(
    *,
    workspace_config=None,
    workspace=None,
    os_backend=None,
    agent_md_path: str | None = None,
    is_tui: bool = False,
    agentsmd_override: bool = False,
    verbose: bool = False,
    logger: Any = None,
) -> PreparedState:
    """Collect project data, identity and metadata before pure State assembly.

    The metadata loader intentionally keeps the existing State semantics: the
    workspace configuration controls project discovery; memory status uses its
    ordinary local defaults. Changing that policy is separate from this extraction.
    ``workspace_config`` — настройки рана: всё, что принимают ``settings_of``/
    ``runtime_of`` (контекст, ``LoadedSettings``).
    """
    from gc_core.utils.project_data import get_project_data

    project_data = await get_project_data(
        workspace_config, workspace=workspace, backend=os_backend
    )
    state_id = str(uuid4())
    status, instructions = load_state_metadata(
        StatePreparationRequest(
            agent_md_path=agent_md_path,
            is_tui=is_tui,
            agentsmd_override=agentsmd_override,
            verbose=verbose,
        ),
        logger=logger,
    )
    return PreparedState(
        state_id=state_id,
        project_data=project_data,
        instructions_text=instructions.text,
        instruction_warnings=list(instructions.warnings),
        long_memory_status=status,
    )
