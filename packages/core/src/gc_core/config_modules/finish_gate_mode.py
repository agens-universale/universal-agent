"""Режим самопроверки на завершении хода — общий тип секций ``tui``,
``acp``, ``chat`` и ``a2a``.

``off`` — судья не зовётся; ``smart`` — только если за ход был вызов
инструмента; ``always`` — всегда. Прежнее имя ``on`` читается как
``always``: старые конфиги и сохранённые настройки сессии грузятся без
правки.
"""

from typing import Annotated, Any, Literal

from pydantic import BeforeValidator

_LEGACY = {"on": "always"}


def _legacy_name(value: Any) -> Any:
    return _LEGACY.get(value, value) if isinstance(value, str) else value


FinishGateMode = Annotated[
    Literal["off", "smart", "always"], BeforeValidator(_legacy_name)
]
