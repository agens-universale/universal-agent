from typing import Dict, Any, List, Type

from tools.subagents.abstract_subagent import AbstractSubagent
from tools.subagents.worker import Worker
from tools.subagents.coder import CodeGenerator
from tools.subagents.planner import Planner
from tools.subagents.critic import Critic
from tools.subagents.code_reviewer import CodeReviewer
from tools.subagents.special_agent import SpecialAgent

SUBAGENT_CLASSES: List[Type[AbstractSubagent]] = [
    Worker,
    CodeGenerator,
    Planner,
    Critic,
    CodeReviewer,
    SpecialAgent,
]

_SUBAGENT_MAP: Dict[str, Type[AbstractSubagent]] = {
    cls.agent_name: cls for cls in SUBAGENT_CLASSES  # type: ignore[attr-defined]
}


def get_subagent_class(tool_name: str) -> Type[AbstractSubagent] | None:
    return _SUBAGENT_MAP.get(tool_name)


def is_subagent_tool(tool_name: str) -> bool:
    return tool_name in _SUBAGENT_MAP


def get_subagents_tools_data() -> Dict[str, Dict[str, Any]]:
    """Собрать tools_data записи для всех субагентов."""
    result: Dict[str, Dict[str, Any]] = {}
    for cls in SUBAGENT_CLASSES:
        spec = cls.get_agent_specification()
        name = spec["name"]
        result[name] = {
            "spec": spec,
            "description": spec.get("description", ""),
            "tool_fabric": None,
        }
    return result
