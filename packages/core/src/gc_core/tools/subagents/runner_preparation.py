"""Explicit preparation boundary for the ordinary SubagentRunner.

Adapters are runtime dependencies, never workflow payloads. Only prepared data
crosses an execution boundary; resolving a prepared definition does not discover
files or import a user-selected Python module.
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol

from gc_core.state_preparation import PreparedState
from gc_core.tools.subagents.custom.file_definitions import SubagentDefinition
from gc_core.tools.subagents.custom.file_subagent import FileDefinedSubagent
from gc_core.tools.subagents.registry import SUBAGENT_CLASSES


@dataclass(frozen=True)
class PreparedChildState:
    #: Настройки ребёнка — всё, что принимает ``settings_of`` (локально —
    #: ``LoadedSettings``). Имя поля прежнее: его знает durable-исполнитель.
    config_dict: Any
    tools_spec: list[dict[str, Any]]
    tools_description: list[Any]
    system_prompt: str | None
    state: PreparedState


def resolve_prepared_subagent(
    tool_name: str, definitions: dict[str, SubagentDefinition]
) -> Any:
    """Bind trusted builtins or an already parsed file definition."""
    for cls in SUBAGENT_CLASSES:
        if cls.agent_name == tool_name:
            return cls
    definition = definitions.get(tool_name)
    if definition is None:
        return None
    if definition.name != tool_name:
        raise ValueError("Prepared subagent definition name mismatch")
    return FileDefinedSubagent.bound(definition)


class SubagentPreparation(Protocol):
    """Host-specific preparation; runner, contexts and classes stay in-process.

    A durable adapter must provide all child model/tool/human/summary ports in
    build_child_context. It must not copy the parent's operation identity onto
    the child. Config must contain the prepared policy for this child, including
    file-definition tool restrictions and the ordinary nesting policy.
    """

    def resolve_subagent(self, tool_name: str) -> Any: ...

    def build_child_config(self, runner: Any) -> Any:
        """Настройки ребёнка: всё, что принимает ``settings_of``."""
        ...

    async def build_child_context(
        self, runner: Any, tool_name: str
    ) -> Any: ...

    async def prepare_child_state(self, runner: Any) -> PreparedChildState: ...

    def release_child_context(self, ctx: Any) -> Awaitable[None] | None: ...
