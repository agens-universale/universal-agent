"""Строки следствий `└` под событием ленты (спека P3 §3).

Раскладка строки — как у строки вызова: `Horizontal(отступ + "└ ", тело)`,
тело в колонке 1fr, поэтому перенос идёт под текстом после `└ `, а не с
первой колонки. Строку собирает `trace_render.build_consequence_line`;
слот держит строки события и пересобирает их только при смене тел."""

from __future__ import annotations

from collections.abc import Iterable

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from gc_tui.widgets.no_select import NoSelectStatic


def _look(body: Text) -> tuple:
    return (body.plain, str(body.style), tuple(body.spans))


class ConsequenceLine(Horizontal):
    """Одна строка `└ тело`. Отступ и `└ ` в выделение не входят; тело —
    по `selectable` (у реплики пользователя следствие в копию не идёт)."""

    DEFAULT_CSS = """
    ConsequenceLine { height: auto; width: 1fr; }
    ConsequenceLine > .consequence-lead { width: auto; height: auto; }
    ConsequenceLine > .consequence-body { width: 1fr; height: auto; }
    """

    def __init__(
        self, lead: Text, body: Text, *, selectable: bool = True
    ) -> None:
        super().__init__()
        self.lead = lead
        self.body = body
        self._selectable = selectable

    @property
    def text(self) -> Text:
        """Строка целиком, как её видно в ленте."""
        return self.lead + self.body

    def compose(self) -> ComposeResult:
        yield NoSelectStatic(
            self.lead, classes="consequence-lead", markup=False
        )
        body_cls = Static if self._selectable else NoSelectStatic
        yield body_cls(self.body, classes="consequence-body", markup=False)


class ConsequenceSlot(Vertical):
    """Строки `└` одного события уровня ``level``, сверху вниз.

    `set_bodies` с теми же телами — no-op: частые обновления владельца
    (тик elapsed, пульс) строки не перемонтируют."""

    DEFAULT_CSS = """
    ConsequenceSlot { height: auto; width: 1fr; }
    """

    def __init__(
        self, level: int, *, selectable: bool = True, **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self._level = level
        self._selectable = selectable
        self._bodies: list[Text] = []
        self._composed = False

    def set_level(self, level: int) -> None:
        """Уровень `└`: у члена блока — под началом его текста."""
        if level == self._level:
            return
        self._level = level
        if self._composed:
            self.remove_children()
            self.mount_all(self._lines())

    def set_bodies(self, bodies: Iterable[Text]) -> None:
        bodies = list(bodies)
        # Сравнение `Text` не видит базового стиля: тот же текст другим
        # цветом (причина отказа от модели блоков) — другое тело.
        if [_look(b) for b in bodies] == [_look(b) for b in self._bodies]:
            return
        self._bodies = bodies
        if self._composed:
            self.remove_children()
            self.mount_all(self._lines())

    def compose(self) -> ComposeResult:
        self._composed = True
        yield from self._lines()

    def _lines(self) -> list[ConsequenceLine]:
        from gc_tui.services.trace_render import build_consequence_line

        return [
            build_consequence_line(
                self._level, body, selectable=self._selectable
            )
            for body in self._bodies
        ]
