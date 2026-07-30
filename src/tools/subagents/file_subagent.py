"""Адаптер: файл-определение → объект, совместимый с ``AbstractSubagent``.

Кастомные сабагенты представлены ДАННЫМИ, а не типами: одного класса достаточно,
он натягивает ``SubagentDefinition`` на существующий контракт. Тело файла играет
роль ``agent_role`` — то есть уезжает в ``<specific_role>`` общего шаблона промпта
через ``render_system_prompt``, а не заменяет шаблон.
"""
from __future__ import annotations

from typing import Any, Dict

from constants.tool_names import FINISH_TOOL_NAME, READ_STORED_TOOL_OUTPUT_NAME
from tools.subagents.abstract_subagent import AbstractSubagent
from tools.subagents.file_definitions import SubagentDefinition, SubagentDefinitionError

# Тулы, без которых сабагент ломается не сразу, а позже и незаметно:
# - turn_finish: контракт сдачи требует вернуть отчёт через него, результат ребёнка
#   извлекается из его вызова, и ядро синтезирует его при cycle_stop. Без него
#   сабагент докручивает до subagent_max_steps и не сдаёт ничего;
# - session_read_stored_tool_output: оборачивание больших выводов безусловно, и в
#   подсказке прямым текстом «Continue with session_read_stored_tool_output».
#   Без тула ребёнку велят вызвать то, чего у него нет.
# Оба отказа выглядят как «модель тупит», поэтому проверяем на старте.
REQUIRED_TOOL_NAMES = frozenset({FINISH_TOOL_NAME, READ_STORED_TOOL_OUTPUT_NAME})


def validate_required_tools(definition: SubagentDefinition) -> None:
    """Убедиться, что определение не отрезает обязательные тулы.

    Единственное место фичи, где мы падаем, а не варним: битый файл лишает
    оператора одного агента, а вынутый ``turn_finish`` даёт агента, который молча
    не работает.
    """
    stop = set(definition.stop_tools)
    allow = set(definition.tools) if definition.tools is not None else None

    # Обе причины собираются целиком и в одно сообщение: иначе оператор чинит
    # файл по одному тулу за прогон, каждый раз перезапуская агента.
    banned = sorted(REQUIRED_TOOL_NAMES & stop)
    missing = sorted(REQUIRED_TOOL_NAMES - allow) if allow is not None else []

    problems = []
    if banned:
        problems.append("нельзя помещать в stop_tools: " + ", ".join(banned))
    if missing:
        problems.append("отсутствуют в списке tools: " + ", ".join(missing))
    if problems:
        raise SubagentDefinitionError(
            definition.source_path,
            "обязательные инструменты — " + "; ".join(problems),
        )


def build_tool_specification(definition: SubagentDefinition) -> Dict[str, Any]:
    """Схема тула для LLM.

    Аргументы фиксированы (``reason`` + ``task``), как у встроенного
    ``subagent_worker``: раннер раскладывает все аргументы кроме ``task`` в kwargs
    ``create_instance``, и произвольная схема потребовала бы менять этот контракт.
    """
    return {
        "type": "function",
        "name": definition.name,
        "description": definition.description,
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "A clear rationale for invoking this agent — why it is being "
                        "used and what objective it helps achieve."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "A detailed description of the task: goal, expected deliverable, "
                        "success criteria, dependencies and constraints."
                    ),
                },
            },
            "required": ["reason", "task"],
            "additionalProperties": False,
        },
        "strict": True,
    }


class FileDefinedSubagent(AbstractSubagent):
    """Сабагент, чья роль задана файлом.

    Класс один на всех кастомных; конкретика живёт в ``definition``. Реестр отдаёт
    не сам класс, а фабрику, замкнутую на определение (см. ``registry``).
    """

    def __init__(self, definition: SubagentDefinition, task: str, reason: str = "") -> None:
        self.definition = definition
        self.task = task
        self.reason = reason

    @classmethod
    async def create_instance(  # type: ignore[override]
        cls,
        definition: SubagentDefinition,
        task: str = "",
        reason: str = "",
        **_kwargs: Any,
    ) -> "FileDefinedSubagent":
        # Прочие kwargs (model, config_dict, runtime_notices_enabled) раннер
        # передаёт всем сабагентам одинаково; файловому они не нужны — модель
        # наследуется от родителя, а LLM-вызова при создании у него нет.
        return cls(definition, task, reason=reason)

    def get_initial_data(self):
        # with_handoff_contract приклеивает WORKFLOW_ASSIGNMENT_GUIDANCE и
        # HANDOFF_CONTRACT_GUIDANCE — кастомная роль получает тот же контракт
        # сдачи, что встроенные. Отменить его из файла нельзя.
        role = self.with_handoff_contract(self.definition.role)
        assignment = self.build_assignment_message(
            {"task": self.task, "reason": self.reason}
        )
        return role, assignment

    def get_agent_name(self) -> str:
        return self.definition.name

    @classmethod
    def get_agent_specification(cls) -> Dict[str, Any]:  # pragma: no cover - см. ниже
        # Спека зависит от определения, а не от класса. Реестр строит её через
        # build_tool_specification(definition); classmethod оставлен только ради
        # контракта AbstractSubagent.
        raise NotImplementedError(
            "спека файлового сабагента строится через build_tool_specification(definition)"
        )


__all__ = [
    "REQUIRED_TOOL_NAMES",
    "FileDefinedSubagent",
    "build_tool_specification",
    "validate_required_tools",
]
