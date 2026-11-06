"""ColumnStatic — `Static` для строк с колонками (`ColumnRow`, `ColumnRows`,
дизайн-код §10), в которых работает выделение мышью.

Textual подсвечивает и копирует выделение только у `Content`; обычный
Rich-объект он рисует как есть и для буфера отдаёт пустоту. Поэтому
строки с колонками раскладываются под фактическую ширину в `Content`:
перенос в своей колонке остаётся, а выделение подсвечивается и копирует
ровно те строки, что видны. Прочее содержимое — как у `Static`.
"""

from __future__ import annotations

from rich.console import Console
from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.text import Text
from textual.content import Content
from textual.css.styles import RulesMap
from textual.selection import Selection
from textual.strip import Strip
from textual.style import Style
from textual.visual import RenderOptions, Visual
from textual.widgets import Static

from gc_tui.services.column_row import ColumnRow, ColumnRows


def _line_text(line: list[Segment]) -> Text:
    """Отрисованная строка обратно в `Text` — без хвостовых пробелов."""
    text = Text.assemble(
        *(
            (seg.text, seg.style or RichStyle())
            for seg in line
            if not seg.control
        )
    )
    text.rstrip()
    return text


class _ColumnVisual(Visual):
    """Строки с колонками, разложенные под ширину в `Content`."""

    def __init__(
        self, renderable: ColumnRow | ColumnRows, console: Console
    ) -> None:
        self._renderable = renderable
        self._console = console
        self._laid: dict[int, Content] = {}

    def laid_out(self, width: int) -> Content:
        if width not in self._laid:
            options = self._console.options.update_width(max(1, width))
            lines = self._console.render_lines(
                self._renderable, options, pad=False, new_lines=False
            )
            text = Text("\n").join(_line_text(line) for line in lines)
            self._laid[width] = Content.from_rich_text(
                text, console=self._console
            )
        return self._laid[width]

    def render_strips(
        self,
        width: int,
        height: int | None,
        style: Style,
        options: RenderOptions,
    ) -> list[Strip]:
        return self.laid_out(width).render_strips(
            width, height, style, options
        )

    def get_optimal_width(self, rules: RulesMap, container_width: int) -> int:
        return self.laid_out(container_width).get_optimal_width(
            rules, container_width
        )

    def get_height(self, rules: RulesMap, width: int) -> int:
        return self.laid_out(width).get_height(rules, width)


class ColumnStatic(Static):
    """`Static`, у которого строки с колонками выделяются и копируются."""

    _column_visual: _ColumnVisual | None = None

    def render(self) -> Visual:
        content = self.content
        if not isinstance(content, (ColumnRow, ColumnRows)):
            return super().render()
        visual = self._column_visual
        if visual is None or visual._renderable is not content:
            visual = _ColumnVisual(content, self.app.console)
            self._column_visual = visual
        return visual

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        visual = self.render()
        if not isinstance(visual, _ColumnVisual):
            return super().get_selection(selection)
        text = visual.laid_out(self.content_region.width).plain
        return selection.extract(text), "\n"
