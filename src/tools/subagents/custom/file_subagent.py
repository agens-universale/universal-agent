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
from tools.subagents.custom.file_definitions import SubagentDefinition

# Служебное ядро: выдаётся сабагенту ВСЕГДА, перечислять в `tools` не нужно, и
# `stop_tools` его не отбирает. Причина в том, что без него сабагент ломается не
# сразу, а позже и незаметно:
# - turn_finish: контракт сдачи требует вернуть отчёт через него, результат ребёнка
#   извлекается из его вызова, и ядро синтезирует его при cycle_stop. Без него
#   сабагент докручивает до subagent_max_steps и не сдаёт ничего;
# - session_read_stored_tool_output: оборачивание больших выводов безусловно, и в
#   подсказке прямым текстом «Continue with session_read_stored_tool_output».
#   Без тула ребёнку велят вызвать то, чего у него нет.
# Оба отказа выглядят как «модель тупит», поэтому ядро не отдаётся на усмотрение
# файла вовсе — это не запрет, а гарантия.
REQUIRED_TOOL_NAMES = frozenset({FINISH_TOOL_NAME, READ_STORED_TOOL_OUTPUT_NAME})


def required_tools_banned_in_definition(definition: SubagentDefinition) -> list[str]:
    """Служебные тулы, которые файл пытается запретить через ``stop_tools``.

    Запрет не исполняется (ядро выдаётся всегда), но и молчать нельзя: оператор
    написал явное указание, и он должен узнать, что оно проигнорировано.
    Отсутствие ядра в ``tools`` сюда НЕ попадает — перечислять его не требуется,
    это штатный путь, а не ошибка.
    """
    return sorted(REQUIRED_TOOL_NAMES & set(definition.stop_tools))


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
    "required_tools_banned_in_definition",
]
