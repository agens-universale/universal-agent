"""Пересборка ленты открытой сессии по окну (спека шага 2 окна ленты,
§3.4, §5.8).

Файл треда подменили снаружи или шаг сжатия не применился — лента
пересобирается синхронно: модель заново, хвост окна, память эфемерного
очищена. Незаконченный ход при пересборке рисует живой путь — тест
`test_rebuild_midrun_unfinished_turn_live` в
`test_session_window_open.py`."""

from __future__ import annotations

import pytest
from feed_dump import feed_dump, without_ephemeral
from gc_testkit.tui_support import wait_until
from test_session_window_open import (
    _SIZE,
    _answer,
    _filled,
    _replies,
    _write_turns,
    small_window,  # noqa: F401 — фикстура
)
from test_trace_ephemera import _SIZE as _EPHEMERA_SIZE
from test_trace_ephemera import _app as _ephemera_app
from test_trace_ephemera import live_session
from test_turn_line_history import _app, _open, _reply

pytestmark = pytest.mark.asyncio


def _ephemeral_rows(scr) -> list:
    rows = feed_dump(scr.query_one("#trace"))
    return [row for row in rows if row not in without_ephemeral(rows)]


@pytest.mark.usefixtures("small_window")
async def test_rebuild_mounts_tail(tmp_path, monkeypatch):
    """Тред вырос снаружи: пересборка монтирует хвост новой истории, не
    всю историю и не прежние виджеты."""
    from gc_tui.screens import trace_window

    thread = _write_turns(tmp_path, 20)
    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        scr = await _open(pilot)
        await _filled(pilot, scr)
        for i in range(20, 40):
            thread.messages(_reply(f"u{i}", f"запрос {i}"), _answer(f"a{i}"))
            thread.record(i % 10, f"u{i}")
        drawn: list[range] = []
        render = trace_window.render_turns

        def spy(host, model, turns, **kw):
            drawn.append(turns)
            return render(host, model, turns, **kw)

        monkeypatch.setattr(trace_window, "render_turns", spy)
        scr._rebuild_from_store()
        await _filled(pilot, scr)

        model = scr._feed_history
        assert model.turn_count() == 40
        assert drawn[0] == range(drawn[0].start, 40)
        assert drawn[0].start > 0
        assert scr._window_first > 0
        replies = _replies(scr)
        assert replies[-1] == "запрос 39"
        assert len(replies) == model.turn_count() - scr._window_first


async def test_rebuild_clears_ephemera(tmp_path):
    """Эфемерное живого хода (мысль, «!», строка команды) после
    пересборки не возвращается: лента — как после открытия, память
    пуста."""
    async with _ephemera_app(tmp_path / "t").run_test(
        size=_EPHEMERA_SIZE
    ) as pilot:
        scr = await live_session(pilot)
        assert _ephemeral_rows(scr)
        scr._rebuild_from_store()
        await pilot.pause()

        assert _ephemeral_rows(scr) == []
        assert scr._ephemera._entries == []
        assert scr._ephemera._decor == []
        assert scr._ephemera.bang_clear(None) == []


async def test_rebuild_after_compaction_failure(tmp_path, monkeypatch):
    """Шаг сжатия не применился — откат той же пересборкой: хвост окна
    из файлов, эфемерное очищено, курсор — за прочитанной историей."""
    async with _ephemera_app(tmp_path / "t").run_test(
        size=_EPHEMERA_SIZE
    ) as pilot:
        scr = await live_session(pilot)

        def broken(_step):
            raise RuntimeError("шаг сжатия сломан")

        monkeypatch.setattr(scr, "_append_compaction_step", broken)
        _segments, cursor = scr._store.read_history(scr._active_task_id)
        scr._apply_compaction(object(), cursor)
        await pilot.pause()

        assert _ephemeral_rows(scr) == []
        assert scr._cursor == cursor
        assert scr._window_heads
        rebuilt = feed_dump(scr.query_one("#trace"))
        scr._rebuild_from_store()
        await pilot.pause()
        assert feed_dump(scr.query_one("#trace")) == rebuilt


async def test_rebuild_label_skips_tail_poll(tmp_path, monkeypatch):
    """Пересборка дольше 2 с (§3.4, §5.8): сначала кадр с неподвижной
    надписью «• обновляю ленту» в баре над вводом, потом пересборка;
    пока она ждёт, тик опроса хвоста не читает тред."""
    from gc_tui.models import TailResult
    from textual.widgets import Static

    _write_turns(tmp_path, 3)
    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        scr = await _open(pilot)
        polled: list[object] = []
        rebuilt: list[object] = []

        def reset_tail(_tid, cursor, *_a):
            polled.append(cursor)
            return TailResult(
                new_records=[], next_cursor=cursor, stream_reset=not rebuilt
            )

        monkeypatch.setattr(scr._store, "tail_events", reset_tail)
        rebuild = scr._rebuild_from_store
        monkeypatch.setattr(
            scr, "_rebuild_from_store", lambda: rebuilt.append(rebuild())
        )
        scr._poll_trace_tail()

        label = scr.query_one("#rebuild-label", Static)
        assert str(label.render()) == "• обновляю ленту"
        assert scr.query_one("#indicator-bar").has_class("-turn")
        assert rebuilt == []
        scr._poll_trace_tail()
        assert len(polled) == 1
        assert rebuilt == []

        await wait_until(pilot, lambda: rebuilt, "пересборка")
        await pilot.pause()
        assert len(rebuilt) == 1
        assert not scr.query("#rebuild-label")
        assert not scr.query_one("#indicator-bar").has_class("-turn")
        assert _replies(scr)[-1] == "запрос 2"
