"""JSON-контракт целого хода в одной activity (без настроек и секретов)."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentActivityRequest:
    agent: str
    prompt: str
    thread_id: str
    system_id: str = "sdk"
    trace_id: str | None = None
    #: Данные приложения для postprocess, например ключ каталога артефактов.
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentActivityResult:
    answer: Any
    #: JSON-совместимый результат postprocess; файлы остаются на исполнителе.
    output: Any = None
