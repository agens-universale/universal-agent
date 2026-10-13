"""Поводы переигрыша слабого ответа: важный тул, битый вызов или отсутствие вызовов.
Цель и повтор запроса принадлежат контроллеру; вердикт не меняет State.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from gc_core.auto_routing.policy import (
    REROUTE_IMPORTANT,
    REROUTE_INVALID_CALL,
    REROUTE_NO_TOOL_CALL,
)
from gc_core.auto_routing.response import has_invalid_call, has_no_tool_call, important_tool_names


@dataclass(frozen=True)
class FloorVerdict:
    """`important_tools` — важные тулы ответа (порядок появления, без дублей);
    `invalid_call` — в ответе битый вызов; `no_tool_call` — в ответе нет вызовов."""
    important_tools: Tuple[str, ...]
    invalid_call: bool
    no_tool_call: bool

    @property
    def fires(self) -> bool:
        return bool(self.triggers)

    @property
    def triggers(self) -> Tuple[str, ...]:
        """Коды сработавших поводов в фиксированном порядке — поле события лога."""
        flags = (
            (REROUTE_IMPORTANT, bool(self.important_tools)),
            (REROUTE_INVALID_CALL, self.invalid_call),
            (REROUTE_NO_TOOL_CALL, self.no_tool_call),
        )
        return tuple(code for code, fired in flags if fired)

    @property
    def tools(self) -> Tuple[str, ...]:
        return self.important_tools

    @property
    def reason(self) -> str:
        """Причина уровня после переигрыша — первый сработавший повод."""
        return self.triggers[0]


def floor_verdict(
    resp_messages: Optional[List[Dict[str, Any]]],
    *,
    tools_data: Optional[Mapping[str, Any]],
    is_subagent: Callable[[str], bool],
) -> Optional[FloorVerdict]:
    """None — поводов нет, ответ остаётся в силе. `tools_data` — реестр тулов
    ЭТОГО такта: важным считается только вызов, чьё имя в нём есть.
    `is_subagent` — предикат тула субагента (реестр субагентов знает контроллер).
    Запуск команд сам по себе важным действием не считается."""
    verdict = FloorVerdict(
        important_tool_names(resp_messages, tools_data, is_subagent=is_subagent),
        has_invalid_call(resp_messages, tools_data),
        has_no_tool_call(resp_messages),
    )
    return verdict if verdict.fires else None
