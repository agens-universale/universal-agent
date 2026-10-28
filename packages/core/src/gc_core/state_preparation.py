"""Local preparation of external values consumed by the existing State.

Only the request and result cross an execution boundary. A caller resolves
workspace, configuration and logging on the side performing preparation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from gc_core.agent_store import AgentStore, agent_store_of
from gc_core.instructions.loader import LoadedInstructions, abuild_instructions
from gc_core.project_memory import aattach_status_to_prompt_vars, project_of


@dataclass(frozen=True)
class StatePreparationRequest:
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


async def aload_state_metadata(
    request: StatePreparationRequest,
    *,
    logger: Any = None,
    metadata_config: Any = None,
    store: AgentStore | None = None,
) -> tuple[str | None, LoadedInstructions]:
    """Read instruction layers and project-memory status in the local runtime.

    ``metadata_config`` — всё, что принимает ``settings_of`` (контекст, настройки).
    Инструкции отдаёт хранилище агента (по умолчанию — его же или файловое).
    """
    variables: dict[str, Any] = {}
    store = store if store is not None else agent_store_of(metadata_config)
    # Проект рана: каталог контекста и agent.project_id (без контекста —
    # текущий каталог).
    project = project_of(metadata_config)
    await aattach_status_to_prompt_vars(
        variables,
        store=store,
        project=project,
        settings=metadata_config,
    )
    instructions = await abuild_instructions(
        is_tui=request.is_tui,
        agentsmd_override=request.agentsmd_override,
        project=project,
        logger=logger,
        verbose=request.verbose,
        store=store,
    )
    return variables.get("longMemoryStatus"), instructions


async def prepare_state(
    *,
    workspace_config=None,
    workspace=None,
    os_backend=None,
    is_tui: bool = False,
    agentsmd_override: bool = False,
    verbose: bool = False,
    logger: Any = None,
    agent_store: AgentStore | None = None,
) -> PreparedState:
    """Collect project data, identity and metadata before pure State assembly.

    The workspace configuration controls project discovery and the
    project-memory status: with ``project_memory`` in ``agent.stop_tools`` the
    prompt does not advertise a memory the agent cannot use (D36).
    ``workspace_config`` — настройки рана: всё, что принимают ``settings_of``/
    ``runtime_of`` (контекст, ``LoadedSettings``). ``agent_store`` — хранилище
    агента; не задано — его же у ``workspace_config`` или файловое.
    """
    from gc_core.utils.project_data import get_project_data

    project_data = await get_project_data(
        workspace_config, workspace=workspace, backend=os_backend
    )
    state_id = str(uuid4())
    status, instructions = await aload_state_metadata(
        StatePreparationRequest(
            is_tui=is_tui,
            agentsmd_override=agentsmd_override,
            verbose=verbose,
        ),
        logger=logger,
        metadata_config=workspace_config,
        store=agent_store
        if agent_store is not None
        else agent_store_of(workspace_config),
    )
    return PreparedState(
        state_id=state_id,
        project_data=project_data,
        instructions_text=instructions.text,
        instruction_warnings=list(instructions.warnings),
        long_memory_status=status,
    )
