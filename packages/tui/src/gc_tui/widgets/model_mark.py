"""Отметка смены модели в живой ленте (спека model-knob §6.4, раунд 3
смока).

Применена — черта во всю ширину по образцу метки «контекст сжат»:
`──── знак ────` (полосы `TRACK`, имя `DEEP_DIM`, точки в цвете уровней).
Выбрана во время хода — знак в колонке глифа с тусклым именем и
следствием `└ модель переключится на ближайшем шаге`, прижата к низу
ленты, как реплика в очереди. Только живая: в тред не пишется, превью её
не рисует."""

from __future__ import annotations

from collections.abc import Mapping

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.widgets import Static

from gc_tui.palette import DEEP_DIM, DIM, MUTED, TRACK
from gc_tui.services import model_sign
from gc_tui.services.trace_render import full_width_rule
from gc_tui.widgets.trace_row import ConsequenceTraceRow
from gc_tui.widgets.user_reply import is_queued

PENDING_NOTE = "модель переключится на ближайшем шаге"
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
            content=self._line(0),
            kind_class="line-model",
            consequences=self._notes(),
        )

    @property
    def choice(self) -> str:
        """Выбор, который показывает отметка."""
        return self._choice

    def set_choice(self, choice: str, alias: str | None) -> None:
        """Новый выбор той же отметки: строка заменяется на месте."""
        self._choice, self._alias = choice, alias
        self._sync()

    def apply(self) -> None:
        """Выбор применён: отметка ожидания становится чертой."""
        self.pending = False
        self._sync()

    def on_mount(self) -> None:
        self._sync()

    def on_resize(self, event: events.Resize) -> None:
        # Черта собирается под ширину строки, как метка сжатия.
        if self.pending or cell_len(self._content.plain) == self.size.width:
            return
        self.set_content(self._line(self.size.width), layout=False)

    def _sync(self) -> None:
        self._prefix = self._glyph()
        self.set_content(self._line(self.size.width))
        if self.is_mounted:
            self.query_one(".trace-prefix", Static).update(self._prefix)
        self._consequences = self._notes()
        self._consequence_slot.set_bodies(self._consequences)

    def _sign(self, name_style: str) -> Text:
        # `auto` — сам выбор, а не модель, которую он взял на этот шаг.
        alias = None if self._choice == _AUTO else self._alias
        return model_sign.sign(
            self._choice, self._slots, name_style=name_style, alias=alias
        )

    def _glyph(self) -> Text:
        """Ожидание — точки знака в колонке глифа; у черты колонки нет."""
        if not self.pending:
            return Text("")
        dots, _ = model_sign.parts(self._sign(DIM))
        return Text.assemble(dots, " ")

    def _line(self, width: int) -> Text:
        if self.pending:
            return model_sign.parts(self._sign(DIM))[1]
        label = Text(style=DEEP_DIM)
        label.append_text(self._sign(DEEP_DIM))
        return full_width_rule(label, width, bar_style=TRACK)

    def _notes(self) -> list[Text]:
        return [Text(PENDING_NOTE, style=MUTED)] if self.pending else []


def pins_to_bottom(widget: object) -> bool:
    """Прижато к низу ленты (спека P3 §2.4, model-knob §6.4): реплика в
    очереди или отметка модели, ждущая ближайшего шага. Только для места
    монтирования; ↑ забирает лишь реплики (`is_queued`)."""
    return is_queued(widget) or (
        isinstance(widget, ModelMark) and widget.pending
    )
