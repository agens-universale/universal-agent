"""Стартовая шапка ленты: блок бренда и живая строка статуса загрузки MCP.

Строку статуса пишет только этот виджет и только по своей фазе: экран
подаёт снимки загрузки (`feed`) и флаг обновления (`set_update`), а что и
когда показать, решает модель «глазка». Таймер анимации живёт, пока идёт
загрузка, и гаснет на итоге и на сбое.
"""

from __future__ import annotations

from collections.abc import Mapping

from textual.message import Message
from textual.timer import Timer

from gc_tui import textual_compat
from gc_tui.branding import BrandHeader, version_label
from gc_tui.services.mcp_peephole import McpPeephole
from gc_tui.services.start_status import status_renderable
from gc_tui.services.trace_glyphs import BLINK_TICK_S
from gc_tui.widgets.column_static import ColumnStatic


class StartHeader(ColumnStatic):
    """Знак, подпись, версия [+ хвост обновления] и строка статуса."""

    class Settled(Message):
        """Сборка поколения закончилась, итог или сбой показан — раз на
        поколение. Без серверов «готов к работе» виден раньше, а сигнал
        ждёт конца сборки: уведомления о тулах появляются при регистрации."""

    def __init__(self, *, mcp_total: int, **kwargs) -> None:
        kwargs.setdefault("id", "brand-block")
        super().__init__(**kwargs)
        self._peephole = McpPeephole(mcp_total)
        self._version = version_label()
        self._update = False
        self._frame = 0
        self._timer: Timer | None = None
        self._settled_generation: int | None = None
        self._redraw()

    def on_mount(self) -> None:
        self._sync_timer()

    def feed(
        self,
        source: object,
        servers: Mapping[str, Mapping | None],
        outcome: bool | None,
    ) -> None:
        """Снимок загрузки MCP; новый `source` — новое поколение."""
        before = self._state()
        self._peephole.sync(source, servers, outcome)
        if self._state() != before:
            self._advance(layout=True)

    def finished(self, source: object) -> bool:
        """Снимки `source` больше не нужны: его загрузка дошла до итога."""
        return self._peephole.finished(source)

    def set_update(self, available: bool) -> None:
        """Хвост «доступна новая версия» в строке версии."""
        if available != self._update:
            self._update = available
            self._redraw()

    def _state(self) -> tuple:
        p = self._peephole
        return (
            p.generation,
            p.phase,
            p.outcome,
            p.total,
            p.connected,
            p.shown,
        )

    def _tick(self) -> None:
        if textual_compat.is_selecting(self.screen):
            return
        phase = self._peephole.phase
        self._peephole.tick()
        self._frame += 1
        # Строка загрузки не переносится: высота меняется только с фазой.
        self._advance(layout=self._peephole.phase != phase)

    def _advance(self, *, layout: bool) -> None:
        self._sync_timer()
        self._redraw(layout=layout)
        self._post_settled()

    def _sync_timer(self) -> None:
        """Идемпотентно: заводит или гасит тик по фазе загрузки."""
        if self._peephole.phase == "loading" and self.is_running:
            if self._timer is None:
                self._timer = self.set_interval(BLINK_TICK_S, self._tick)
        elif self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _post_settled(self) -> None:
        p = self._peephole
        if (
            p.phase != "loading"
            and p.outcome is not None
            and self._settled_generation != p.generation
        ):
            self._settled_generation = p.generation
            self.post_message(self.Settled())

    def _redraw(self, *, layout: bool = True) -> None:
        status = status_renderable(self._peephole, self._frame)
        header = BrandHeader(self._version, update=self._update, status=status)
        self.update(header, layout=layout)
