"""ReservedHint: высота — под самый длинный вариант при своей ширине,
смена текста её не меняет."""

from __future__ import annotations

import pytest

_SHORT = "коротко"
_LONG = "длинная подсказка " * 6


def _app(hint):
    from textual.app import App
    from textual.containers import Vertical

    class _A(App):
        CSS = "ReservedHint { padding: 0 1; }"

        def compose(self):
            yield Vertical(hint, id="frame")

    return _A()


@pytest.mark.asyncio
async def test_height_reserved_for_longest_variant():
    from gc_tui.widgets.reserved_hint import ReservedHint

    hint = ReservedHint(_SHORT, variants=lambda: [_SHORT, _LONG])
    async with _app(hint).run_test(size=(42, 20)) as pilot:
        await pilot.pause()
        # 40 клеток текста: длинный вариант (~108 знаков) — в 3 строки.
        assert hint.size.height == 3
        hint.update(_LONG)
        await pilot.pause()
        assert hint.size.height == 3
        hint.update(_SHORT)
        await pilot.pause()
        assert hint.size.height == 3


@pytest.mark.asyncio
async def test_height_follows_width():
    from gc_tui.widgets.reserved_hint import ReservedHint

    hint = ReservedHint(_SHORT, variants=lambda: [_SHORT, _LONG])
    async with _app(hint).run_test(size=(42, 20)) as pilot:
        await pilot.pause()
        narrow = hint.size.height
        await pilot.resize_terminal(122, 20)
        await pilot.pause()
        assert hint.size.height < narrow
