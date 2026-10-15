"""Рамка плана в ленте: предложенный план серый, утверждённый — цвета режима plan.

Рамка сама знает текст своего плана: состояние меняется у всех рамок с текстом
действующего плана, найденных в ленте в этот момент, — и у исходной, и у копий,
показанных командой `/plan`. Экран ссылок на рамки не хранит.
"""

from __future__ import annotations

from textual.containers import Vertical

from gc_tui.services.mode_pill import GLYPH_PLAN

_TITLE_PROPOSED = f"{GLYPH_PLAN} предложенный план"
_TITLE_ACCEPTED = f"{GLYPH_PLAN} утверждённый план"


class PlanPanel(Vertical):
    def __init__(self, *children, plan_text: str, **kwargs) -> None:
        super().__init__(*children, classes="plan-panel", **kwargs)
        self.plan_text = plan_text.strip()
        self.border_title = _TITLE_PROPOSED

    def shows(self, plan_text: str) -> bool:
        """Рамка показывает именно этот текст плана."""
        return self.plan_text == str(plan_text or "").strip()

    def set_accepted(self, accepted: bool) -> None:
        self.set_class(accepted, "plan-accepted")
        self.border_title = _TITLE_ACCEPTED if accepted else _TITLE_PROPOSED
