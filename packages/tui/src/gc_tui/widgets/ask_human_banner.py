"""AskHumanBanner — вопрос агента, sticky-блок над полем ввода (canon §3.4)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static


def format_ask_label(via: str | None) -> str:
    base = "? АГЕНТ СПРАШИВАЕТ"
    if via:
        return f"{base} · via {via}"
    return base


class AskHumanBanner(VerticalScroll):
    """Banner с вопросом агента.

    VerticalScroll + max-height: длинный вопрос скроллится внутри баннера,
    не выдавливает поле ввода и футер (см. _sync_trace_max_height).

    Тон ink/bright (НЕ violet — violet только в empty-state).
    Стили — `.ask-human-banner` в styles.tcss.
    """

    DEFAULT_CSS = ""  # стили в styles.tcss
    # Фокус остаётся в поле ответа; колёсико над баннером всё равно скроллит.
    can_focus = False

    def __init__(
        self, *, question: str, via: str | None = None, **kwargs
    ) -> None:
        kwargs.setdefault("classes", "")
        kwargs["classes"] = ("ask-human-banner " + kwargs["classes"]).strip()
        super().__init__(**kwargs)
        self._question = question
        self._via = via

    def compose(self) -> ComposeResult:
        from gc_tui.services.trace_render import build_block_widgets

        yield Static(
            format_ask_label(self._via), classes="ah-label", markup=False
        )
        yield from build_block_widgets(
            self._question, prose_bullet=False, event_no=0
        )
