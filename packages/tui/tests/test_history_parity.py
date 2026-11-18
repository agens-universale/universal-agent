"""Паритет отрисовщика кусков истории с живым путём (спека шага 2 окна
ленты, §5.2, §8 «Паритет»).

Все ходы модели, нарисованные `render_turns`, дают ту же ленту, что те же
записи, поданные живым путём, за вычетом эфемерного; ходы по одному подряд
— то же, что все разом. Отрисовщик здесь без узла вложенного трейса:
вложенные строки субагента — вне сравнения (их паритет — в тестах узла)."""

from __future__ import annotations

import pytest
from feed_dump import (
    HISTORY_THREAD,
    load_history_fixture,
    without_ephemeral,
    write_history,
)
from gc_testkit.tui_support import pinned_snapshot_clock
from test_feed_parity_baseline import (
    _SIZE,
    HISTORY_FIXTURES,
    _app,
    _settled_dump,
    dump_after_live_feed,
)


def _chunk_options(screen):
    from gc_tui.services.history_render import ChunkOptions

    mcp_tools, mcp_servers = screen._mcp_tool_info()
    return ChunkOptions(
        focusable_compaction=True,
        mcp_tools=mcp_tools,
        mcp_servers=mcp_servers,
        model_slots=screen._footer_slots(),
    )


async def dump_after_render(name: str, tmp_path, *, one_by_one: bool):
    """Тред фикстуры записан целиком; модель из него — в ленту экрана
    отрисовщиком кусков (экран тред не открывает)."""
    from gc_tui.services.history_model import build_history
    from gc_tui.services.history_render import FeedHost
    from trace_readonly import render_turns
    from gc_tui.services.stores.file_store import FileTaskStore

    threads = tmp_path / "chunks"
    async with _app(threads).run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        write_history(str(threads), load_history_fixture(name))
        segments, _cursor = FileTaskStore(
            threads_dir=str(threads), system="cli"
        ).read_history(HISTORY_THREAD)
        model = build_history(segments, inputs=scr._history_inputs())
        host = FeedHost(scr.query_one("#trace"))
        options = _chunk_options(scr)
        count = model.turn_count()
        parts = [range(i, i + 1) for i in range(count)]
        for turns in parts if one_by_one else [range(count)]:
            render_turns(host, model, turns, before=None, options=options)
        return await _settled_dump(pilot)


def _without_nested(rows: list) -> list:
    """Отрисовщик здесь без узла вложенного трейса: вложенные строки
    (`-in-agent`) вне сравнения, их паритет — в тестах узла."""
    return [row for row in rows if "-in-agent" not in row[2]]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", HISTORY_FIXTURES)
@pytest.mark.parametrize("splits", [(), (3,), (1, 4, 7)])
async def test_render_turns_matches_live_feed(
    name, splits, tmp_path, monkeypatch
):
    with pinned_snapshot_clock(monkeypatch):
        rendered = await dump_after_render(name, tmp_path, one_by_one=False)
        live = await dump_after_live_feed(name, tmp_path, splits=splits)
    assert rendered == _without_nested(without_ephemeral(live))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", HISTORY_FIXTURES)
async def test_turn_by_turn_matches_all_at_once(name, tmp_path, monkeypatch):
    (tmp_path / "parts").mkdir()
    with pinned_snapshot_clock(monkeypatch):
        whole = await dump_after_render(name, tmp_path, one_by_one=False)
        parts = await dump_after_render(
            name, tmp_path / "parts", one_by_one=True
        )
    assert parts == whole
