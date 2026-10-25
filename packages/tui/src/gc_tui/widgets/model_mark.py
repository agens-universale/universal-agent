"""Отметка смены модели в живой ленте (спека model-knob §6.4).

Событие уровня 0, глиф в колонке `●`: применена — `✦ Model  знак`,
выбрана во время хода — тихая `✧ Model  знак` со следствием
`└ переключится на ближайшем шаге`, прижата к низу ленты, как реплика в
очереди. Только живая: в тред не пишется, превью её не рисует."""

from __future__ import annotations

from collections.abc import Mapping

from rich.text import Text
from textual.widgets import Static

from gc_tui.palette import DIM, INK, MUTED, SUB_INK
from gc_tui.services import model_sign
from gc_tui.widgets.trace_row import ConsequenceTraceRow
from gc_tui.widgets.user_reply import is_queued

PENDING_NOTE = "переключится на ближайшем шаге"
_LABEL = "Model"
_AUTO = "auto"


class ModelMark(ConsequenceTraceRow):
    """Строка смены модели; `pending` — выбор ещё ждёт ближайшего шага."""

    def __init__(
        self,
        choice: str,
        slots: Mapping[str, str | None],
        *,
        alias: str | None = None,
        pending: bool,
    ) -> None:
        self._choice = choice
        self._alias = alias
        self._slots = slots
        self.pending = pending
        super().__init__(
            prefix=self._glyph(),
            content=self._line(),
            kind_class="line-model",
            consequences=self._notes(),
        )

    @property
    def choice(self) -> str:
        """Выбор, который показывает отметка."""
        return self._choice

    def set_choice(self, choice: str, alias: str | None) -> None:
        """Новый выбор того же ожидания: строка заменяется на месте."""
        self._choice, self._alias = choice, alias
        self._sync()

    def apply(self) -> None:
        """Выбор применён: тихая отметка становится обычной."""
        self.pending = False
        self._sync()

    def _sync(self) -> None:
        self._prefix = self._glyph()
        self.set_content(self._line())
        if self.is_mounted:
            self.query_one(".trace-prefix", Static).update(self._prefix)
        self._consequences = self._notes()
        self._consequence_slot.set_bodies(self._consequences)

    def _glyph(self) -> Text:
        if self.pending:
            return Text("✧ ", style=MUTED)
        return Text("✦ ", style=SUB_INK)

    def _line(self) -> Text:
        label = DIM if self.pending else SUB_INK
        # `auto` — сам выбор, а не модель, которую он взял на этот шаг.
        alias = None if self._choice == _AUTO else self._alias
        sign = model_sign.sign(
            self._choice,
            self._slots,
            name_style=DIM if self.pending else INK,
            alias=alias,
        )
        return Text.assemble((_LABEL, label), "  ", sign)

    def _notes(self) -> list[Text]:
        return [Text(PENDING_NOTE, style=MUTED)] if self.pending else []


def pins_to_bottom(widget: object) -> bool:
    """Прижато к низу ленты (спека P3 §2.4, model-knob §6.4): реплика в
    очереди или тихая отметка модели. Только для места монтирования; ↑
    забирает лишь реплики (`is_queued`)."""
    return is_queued(widget) or (
        isinstance(widget, ModelMark) and widget.pending
    )
