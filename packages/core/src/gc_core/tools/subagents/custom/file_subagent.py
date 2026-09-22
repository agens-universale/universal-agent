"""Адаптер: файл-определение → bound handle + runtime instance.

Кастомные сабагенты представлены ДАННЫМИ, а не типами: одного класса достаточно,
он натягивает ``SubagentDefinition`` на существующий контракт. Тело файла играет
роль ``agent_role`` — то есть уезжает в ``<specific_role>`` общего шаблона промпта
через ``render_system_prompt``, а не заменяет шаблон.

API адаптера собран на классе + ``BoundFileSubagent``: schema, required-tools
policy, bind, create, instance methods. Registry только compose'ит через
``FileDefinedSubagent.bound(definition)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from gc_core.constants.tool_names import (
    FINISH_TOOL_NAME,
    READ_STORED_TOOL_OUTPUT_NAME,
    SESSION_MEMORY_TOOL_NAME,
    SESSION_TODO_TOOL_NAME,
)
from gc_core.tools.subagents.abstract_subagent import AbstractSubagent
from gc_core.tools.subagents.custom.file_definitions import SubagentDefinition


class FileDefinedSubagent(AbstractSubagent):
    """Сабагент, чья роль задана файлом.

    Класс один на всех кастомных; конкретика живёт в ``definition``. Реестр
    получает не сам класс, а bound-handle (см. ``bound``), замкнутый на
    конкретное определение.
    """

    # Служебное ядро: выдаётся сабагенту ВСЕГДА, перечислять в `tools` не нужно, и
    # `stop_tools` его не отбирает. Причина в том, что без него сабагент ломается не
    # сразу, а позже и незаметно:
    # - turn_finish: контракт сдачи требует вернуть отчёт через него — результат
    #   ребёнка извлекается из его вызова. Без него сабагент докручивает до
    #   subagent_max_steps и не сдаёт ничего;
    # - session_read_stored_tool_output: оборачивание больших выводов безусловно, и в
    #   подсказке прямым текстом «Continue with session_read_stored_tool_output».
    #   Без тула ребёнку велят вызвать то, чего у него нет;
    # - session_memory: харнес шлёт ребёнку bootstrap памяти на первом же шаге
    #   (``runner.py``: «update persistent registers with the thread memory tool»)
    #   и периодический tool_usage_notice. Оба текста рождаются из ГЛОБАЛЬНОГО
    #   конфига и про `tools` файла не знают — без тула ребёнку снова велят вызвать
    #   то, чего у него нет;
    # - session_todo: тот же класс отказа — todo_reminder зовёт завести чек-лист,
    #   сверяясь только с глобальным stop_tools.
    # Все отказы выглядят как «модель тупит», поэтому ядро не отдаётся на усмотрение
    # файла вовсе — это не запрет, а гарантия.
    # Канон здесь, не в file_definitions: это runtime policy сдачи/stored output/памяти.
    REQUIRED_TOOL_NAMES = frozenset(
        {
            FINISH_TOOL_NAME,
            READ_STORED_TOOL_OUTPUT_NAME,
            SESSION_MEMORY_TOOL_NAME,
            SESSION_TODO_TOOL_NAME,
        }
    )

    def __init__(self, definition: SubagentDefinition, task: str, reason: str = "") -> None:
        self.definition = definition
        self.task = task
        self.reason = reason

    @classmethod
    def bound(cls, definition: SubagentDefinition) -> BoundFileSubagent:
        """Definition, замкнутое на контракт раннера/реестра.

        Раннер использует только ``create_instance``; registry — ещё
        ``get_agent_specification`` и ``agent_name``. Полноценный
        ``Type[AbstractSubagent]`` на каждое md-определение не нужен —
        достаточно bound-handle.
        """
        return BoundFileSubagent(definition)

    @classmethod
    def required_tools_banned(cls, definition: SubagentDefinition) -> list[str]:
        """Служебные тулы, которые файл пытается запретить через ``stop_tools``.

        Запрет не исполняется (ядро выдаётся всегда), но и молчать нельзя: оператор
        написал явное указание, и он должен узнать, что оно проигнорировано.
        Отсутствие ядра в ``tools`` сюда НЕ попадает — перечислять его не требуется,
        это штатный путь, а не ошибка.
        """
        return sorted(cls.REQUIRED_TOOL_NAMES & set(definition.stop_tools))

    @staticmethod
    def tool_specification(definition: SubagentDefinition) -> Dict[str, Any]:
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

    @classmethod
    async def create_instance(  # type: ignore[override]
        cls,
        definition: SubagentDefinition,
        task: str = "",
        reason: str = "",
        **_kwargs: Any,
    ) -> FileDefinedSubagent:
        # Прочие kwargs (model, config_dict, runtime_notices_enabled) раннер
        # передаёт всем сабагентам одинаково; файловому они не нужны — модель
        # наследуется от родителя, а LLM-вызова при создании у него нет.
        return cls(definition, task, reason=reason)

    def get_initial_data(self):
        # with_handoff_contract приклеивает WORKFLOW_ASSIGNMENT_GUIDANCE и
        # HANDOFF_CONTRACT_GUIDANCE — кастомная роль получает тот же контракт
        # сдачи, что встроенные. Отменить его из файла нельзя.
        role = self.with_handoff_contract(self.definition.role)
        assignment = self.build_assignment_message({"task": self.task, "reason": self.reason})
        return role, assignment

    def get_agent_name(self) -> str:
        return self.definition.name

    @classmethod
    def get_agent_specification(cls) -> Dict[str, Any]:  # pragma: no cover - см. ниже
        # Спека зависит от определения, а не от класса. Основной production-путь —
        # BoundFileSubagent.get_agent_specification() через bound(). Classmethod
        # оставлен только ради контракта AbstractSubagent.
        raise NotImplementedError(
            "FileDefinedSubagent is data-driven; use FileDefinedSubagent.bound(definition).get_agent_specification()"
        )


@dataclass(frozen=True)
class BoundFileSubagent:
    """Definition, замкнутое на контракт раннера/реестра.

    Раннер использует только ``create_instance``; registry — ещё
    ``get_agent_specification`` и ``agent_name``. Полноценный
    ``Type[AbstractSubagent]`` на каждое md-определение не нужен.
    """

    definition: SubagentDefinition

    @property
    def agent_name(self) -> str:
        return self.definition.name

    def get_agent_specification(self) -> Dict[str, Any]:
        return FileDefinedSubagent.tool_specification(self.definition)

    async def create_instance(self, **kwargs: Any) -> FileDefinedSubagent:
        kwargs.pop("definition", None)  # definition только из bound
        return await FileDefinedSubagent.create_instance(definition=self.definition, **kwargs)


# Module-level re-export для короткого импорта в tools/__init__.py (child filter).
REQUIRED_TOOL_NAMES = FileDefinedSubagent.REQUIRED_TOOL_NAMES

__all__ = [
    "REQUIRED_TOOL_NAMES",
    "BoundFileSubagent",
    "FileDefinedSubagent",
]
