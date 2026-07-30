from typing import Any, Dict, List, Optional, Type

from tools.subagents.abstract_subagent import AbstractSubagent
from tools.subagents.worker import Worker
from tools.subagents.coder import CodeGenerator
from tools.subagents.code_planner import CodePlanner
from tools.subagents.planner import Planner
from tools.subagents.critic import Critic
from tools.subagents.code_reviewer import CodeReviewer
from tools.subagents.special_agent import SpecialAgent
from tools.subagents.custom.file_definitions import (
    SubagentDefinition,
    discover_subagent_definitions,
)
from tools.subagents.custom.file_subagent import (
    FileDefinedSubagent,
    build_tool_specification,
    validate_required_tools,
)

SUBAGENT_CLASSES: List[Type[AbstractSubagent]] = [
    Worker,
    CodeGenerator,
    CodePlanner,
    Planner,
    Critic,
    CodeReviewer,
    SpecialAgent,
]

_SUBAGENT_MAP: Dict[str, Type[AbstractSubagent]] = {
    cls.agent_name: cls for cls in SUBAGENT_CLASSES  # type: ignore[attr-defined]
}

# Разобранные определения кэшируются в самом config_dict: реестр дёргается на
# КАЖДЫЙ вызов тула (диспетчер apply_tool), и читать папку каждый раз было бы
# расточительно. Кэш в config_dict, а не глобальный: в серверном режиме раны идут
# в разных потоках со своими конфигами, и общий кэш отдавал бы чужие определения.
_DEFS_CACHE_KEY = "_subagent_definitions_cache"


def builtin_subagent_names() -> set:
    """Имена встроенных сабагентов — они выигрывают коллизию у кастомных."""
    return set(_SUBAGENT_MAP.keys())


class _FileSubagentFactory:
    """Замыкание определения на контракт ``create_instance``.

    Раннер обращается к результату ``get_subagent_class`` только за
    ``create_instance`` (``runner.py:124``), поэтому полноценный класс на каждое
    определение не нужен — достаточно объекта с этим методом.
    """

    def __init__(self, definition: SubagentDefinition) -> None:
        self.definition = definition
        self.agent_name = definition.name

    async def create_instance(self, **kwargs: Any) -> FileDefinedSubagent:
        kwargs.pop("definition", None)
        return await FileDefinedSubagent.create_instance(
            definition=self.definition, **kwargs
        )


def resolve_subagent_definitions(
    config_dict: Optional[Dict[str, Any]] = None,
    *,
    logger: Any = None,
) -> Dict[str, SubagentDefinition]:
    """Определения кастомных сабагентов для этого конфига (с мемоизацией).

    ``config_dict is None`` → кастомных нет вовсе: так вызывают тесты и любой
    код, которому нужен только встроенный состав.

    Проверка обязательных тулов делается ЗДЕСЬ и намеренно не перехватывается:
    битый файл лишает оператора одного агента (варнинг), а вынутый ``turn_finish``
    даёт агента, который молча не работает, — это ошибка старта.
    """
    if config_dict is None:
        return {}

    cached = config_dict.get(_DEFS_CACHE_KEY)
    if isinstance(cached, dict):
        return cached

    definitions = discover_subagent_definitions(
        config_dict, builtin_names=builtin_subagent_names(), logger=logger
    )
    for definition in definitions:
        validate_required_tools(definition)

    resolved = {definition.name: definition for definition in definitions}
    config_dict[_DEFS_CACHE_KEY] = resolved
    return resolved


def get_subagent_class(
    tool_name: str, config_dict: Optional[Dict[str, Any]] = None
) -> Any:
    builtin = _SUBAGENT_MAP.get(tool_name)
    if builtin is not None:
        return builtin
    definition = resolve_subagent_definitions(config_dict).get(tool_name)
    return _FileSubagentFactory(definition) if definition is not None else None


def is_subagent_tool(
    tool_name: str, config_dict: Optional[Dict[str, Any]] = None
) -> bool:
    if tool_name in _SUBAGENT_MAP:
        return True
    return tool_name in resolve_subagent_definitions(config_dict)


def get_subagents_tools_data(
    config_dict: Optional[Dict[str, Any]] = None,
    *,
    logger: Any = None,
) -> Dict[str, Dict[str, Any]]:
    """Собрать tools_data записи для встроенных и кастомных сабагентов."""
    result: Dict[str, Dict[str, Any]] = {}
    for cls in SUBAGENT_CLASSES:
        spec = cls.get_agent_specification()
        result[spec["name"]] = {
            "spec": spec,
            "description": spec.get("description", ""),
            "tool_fabric": None,
        }

    for name, definition in resolve_subagent_definitions(
        config_dict, logger=logger
    ).items():
        spec = build_tool_specification(definition)
        result[name] = {
            "spec": spec,
            "description": definition.description,
            "tool_fabric": None,
        }
    return result
