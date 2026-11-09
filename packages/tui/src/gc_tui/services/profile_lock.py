"""Смена профиля во время хода закрыта.

Профиль действует только со следующего хода (пересборка реестра моделей и
MCP), и выбор посреди хода расходился бы с ходом: футер называл бы новый
профиль, а ход шёл на старом. Здесь — один на все входы предикат и фраза
отказа: пикер `/profile`, набранная команда, «Настройки → Профили»."""

from __future__ import annotations

from typing import Any, Optional

PROFILE_LOCKED_REASON = "профиль можно сменить, когда ход закончится"


def profile_switch_refusal(app: Any) -> Optional[str]:
    """Причина, по которой профиль сейчас не сменить; None — можно."""
    runner = getattr(app, "active_runner", None)
    if runner is not None and runner.is_running():
        return PROFILE_LOCKED_REASON
    return None
