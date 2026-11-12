"""Читатели треда TUI и поток итогов хода (``turns``).

Итог хода — запись своего потока, привязанная к последнему сообщению
хода (``after``). Хранилище отдаёт потоки пачками по потоку, поэтому
читатель ставит запись по привязке, а не по месту в пачке: за её
сообщением, а сообщения нет в пачке (прочитано раньше) — в начало пачки.
Запись без сообщения своего хода в истории (откат, сброс) — сирота, её
не видно. Конверт записи — ``type: "turn_outcome"``: счёт и объём
сегмента сжатия, заголовок экспорта и ``_payload_ids`` её не считают;
лента и превью — те же виджеты сообщений плюс строка хода.
"""

from __future__ import annotations

import json

import pytest
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_DIALOG_SUMMARY,
    wrap_technical_user_content,
)
from gc_core.persistence.turn_outcome import TurnOutcome
from gc_core.threads.facade import Threads, run_sync
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_testkit.thread_writes import turn_outcome_record
from gc_testkit.tui_support import compact_thread, tui_settings
from gc_tui.services.export import export_thread
from gc_tui.services.stores.file_store import FileTaskStore, _payload_ids

_SYS = "cli"
_TID = "t1"
_KEY = ThreadKey(system=_SYS, thread=_TID)


def _outcome(opened_by: str = "m1") -> TurnOutcome:
    return TurnOutcome(
        status="done",
        reason=None,
        opened_by=opened_by,
        started_at="2026-10-22T11:32:00.000Z",
        ended_at="2026-10-22T11:32:12.000Z",
        duration_sec=12.0,
        error=None,
    )


def _user(mid: str, text: str = "вопрос") -> dict:
    return {"role": "user", "content": f"{mid} {text}", "id": mid}


def _assistant(mid: str, text: str = "ответ") -> dict:
    return {"role": "assistant", "content": text, "id": mid}


def _summary(suffix: str) -> dict:
    return {
        "role": "user",
        "content": wrap_technical_user_content(
            "Summarized context from previous dialogue.\nкратко",
            technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY,
        ),
        "id": f"sum_{suffix}",
    }


class _Thread:
    def __init__(self, tmp_path) -> None:
        self.dir = str(tmp_path)

    def append(self, *msgs: dict) -> None:
        threads = Threads(FileThreadStore(self.dir))
        run_sync(threads.append_messages(_KEY, list(msgs)))

    def outcome(self, opened_by: str = "m1") -> None:
        turn_outcome_record(self.dir, _SYS, _TID, _outcome(opened_by))

    def discard(self, mid: str) -> None:
        threads = Threads(FileThreadStore(self.dir))
        assert run_sync(threads.discard_message(_KEY, mid))

    def compact(self, suffix: str) -> None:
        compact_thread(self.dir, _SYS, _TID, [_summary(suffix)])


def _store(thread: _Thread) -> FileTaskStore:
    return FileTaskStore(thread.dir, system=_SYS)


def _ids(records: list[dict]) -> list:
    return [(r.get("payload") or {}).get("id", r.get("type")) for r in records]


# ---- хвост ----------------------------------------------------------------


def test_record_read_after_its_turn_leads_the_tail(tmp_path):
    thread = _Thread(tmp_path)
    thread.append(_user("m1"), _assistant("m2"))
    store = _store(thread)
    _, cur = store.read_all_events(_TID)

    thread.outcome()
    thread.append(_user("m3"))
    tail = store.tail_events(_TID, cur)

    assert tail.stream_reset is False
    assert _ids(tail.new_records) == ["turn_outcome", "m3"]
    assert tail.new_records[0]["after"] == "m2"
    tail = store.tail_events(_TID, tail.next_cursor)
    assert tail.new_records == []


def test_record_in_one_batch_with_next_turn_follows_its_anchor(tmp_path):
    thread = _Thread(tmp_path)
    store = _store(thread)
    _, cur = store.read_all_events(_TID)

    thread.append(_user("m1"), _assistant("m2"))
    thread.outcome()
    thread.append(_user("m3"))
    tail = store.tail_events(_TID, cur)

    assert _ids(tail.new_records) == ["m1", "m2", "turn_outcome", "m3"]


def test_full_read_places_each_record_after_its_turn(tmp_path):
    thread = _Thread(tmp_path)
    thread.append(_user("m1"), _assistant("m2"))
    thread.outcome("m1")
    thread.append(_user("m3"), _assistant("m4"))
    thread.outcome("m3")

    records, _ = _store(thread).read_all_events(_TID)

    assert _ids(records) == [
        "m1",
        "m2",
        "turn_outcome",
        "m3",
        "m4",
        "turn_outcome",
    ]


def test_full_read_drops_record_of_undone_reply(tmp_path):
    thread = _Thread(tmp_path)
    thread.append(_user("m1"), _assistant("m2"))
    thread.outcome("m1")
    thread.append(_user("m3"))
    thread.outcome("m3")
    thread.discard("m3")

    records, _ = _store(thread).read_all_events(_TID)

    assert _ids(records) == ["m1", "m2", "turn_outcome"]


def test_sessions_list_after_undo_shows_previous_turn(tmp_path):
    """Откат реплики остановленного хода: список сессий показывает итог
    прошлого хода, как и лента (запись снятого хода — сирота)."""
    thread = _Thread(tmp_path)
    thread.append(_user("m1"), _assistant("a1"))
    thread.outcome("m1")
    thread.append(_user("m2"))
    stopped = TurnOutcome(
        status="stopped",
        reason="user_stop",
        opened_by="m2",
        started_at="2026-10-22T11:33:00.000Z",
        ended_at="2026-10-22T11:33:01.000Z",
        duration_sec=1.0,
        error=None,
    )
    turn_outcome_record(thread.dir, _SYS, _TID, stopped)
    store = _store(thread)
    assert [s.status for s in store.list_tasks()] == ["stopped"]

    assert store.discard_message_by_id(_TID, "m2")
    assert [s.status for s in store.list_tasks()] == ["completed"]


def test_compaction_after_record_is_a_step_not_reset(tmp_path):
    thread = _Thread(tmp_path)
    thread.append(_user("m1"), _assistant("m2"))
    store = _store(thread)
    _, cur = store.read_all_events(_TID)

    thread.outcome()
    thread.compact("S")
    tail = store.tail_events(_TID, cur)

    assert tail.stream_reset is False
    assert tail.compaction is not None
    assert tail.compaction.compacted_by["messages_in_segment"] == 2
    # Запись хода дочитывается следующим тиком, один раз.
    tail = store.tail_events(_TID, tail.next_cursor)
    assert _ids(tail.new_records) == ["turn_outcome"]
    assert store.tail_events(_TID, tail.next_cursor).new_records == []


def test_segment_counts_only_messages(tmp_path):
    thread = _Thread(tmp_path)
    thread.append(_user("m1"), _assistant("m2"))
    thread.outcome()
    thread.compact("S")
    thread.append(_user("m3"))

    segments = _store(thread).read_segments(_TID)

    assert segments[0]["compacted_by"]["messages_in_segment"] == 2
    assert _ids(segments[0]["messages"]) == ["m1", "m2", "turn_outcome"]
    assert _ids(segments[1]["messages"]) == ["m3"]


def test_segments_drop_orphan_record(tmp_path):
    thread = _Thread(tmp_path)
    thread.append(_user("m1"))
    thread.outcome("m_gone")

    (segment,) = _store(thread).read_segments(_TID)

    assert _ids(segment["messages"]) == ["m1"]


def test_payload_ids_ignore_record(tmp_path):
    thread = _Thread(tmp_path)
    thread.append(_user("m1"))
    thread.outcome()
    records, _ = _store(thread).read_all_events(_TID)

    assert _ids(records) == ["m1", "turn_outcome"]
    assert _payload_ids(records) == frozenset({"m1"})


# ---- экспорт --------------------------------------------------------------


def _export(thread: _Thread, fmt: str) -> str:
    store = _store(thread)
    return export_thread(
        store.read_segments(_TID),
        fmt=fmt,
        api_mode="",
        thread_id=_TID,
        system=_SYS,
    )


def test_export_skips_record(tmp_path):
    with_record = _Thread(tmp_path)
    with_record.append(_user("m1"), _assistant("m2"))
    with_record.outcome()

    data = json.loads(_export(with_record, "json"))
    assert data["thread"]["messages_count"] == 2
    assert [m["id"] for m in data["segments"][0]["messages"]] == ["m1", "m2"]
    txt = _export(with_record, "txt")
    assert "2 messages" in txt
    assert "turn_outcome" not in txt


def test_export_ts_last_is_last_message(tmp_path):
    thread = _Thread(tmp_path)
    thread.append(_user("m1"))
    records_before, _ = _store(thread).read_all_events(_TID)
    thread.outcome()

    data = json.loads(_export(thread, "json"))
    assert data["thread"]["ts_last"] == records_before[-1]["ts"]


# ---- лента и превью -------------------------------------------------------


def _records(tmp_path, *, with_record: bool) -> list[dict]:
    thread = _Thread(tmp_path)
    thread.append(_user("m1"), _assistant("m2"))
    if with_record:
        thread.outcome()
    thread.append(_user("m3", "ещё"), _assistant("m4", "ещё ответ"))
    records, _ = _store(thread).read_all_events(_TID)
    return records


def _text(widget) -> str:
    rendered = widget.render()
    if isinstance(rendered, str):
        return rendered
    return getattr(rendered, "plain", type(rendered).__name__)


def _widgets(root) -> list[tuple[str, str]]:
    """Виджеты сообщений: строка хода — не сообщение, её считает
    `_turn_lines`."""
    from gc_tui.widgets.turn_line import TurnLine

    return [
        (type(w).__name__, _text(w))
        for w in root.query("*")
        if not isinstance(w, TurnLine)
    ]


def _turn_lines(root) -> int:
    from gc_tui.widgets.turn_line import TurnLine

    return len(root.query(TurnLine))


@pytest.mark.asyncio
async def test_live_feed_ignores_record(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    seen, lines = [], []
    for with_record in (False, True):
        root = tmp_path / str(with_record)
        records = _records(root, with_record=with_record)
        app = TuiApp(
            threads_dir=str(root),
            system=_SYS,
            settings=tui_settings({"agent.model": "M"}),
        )
        async with app.run_test() as pilot:
            scr = pilot.app.screen
            scr._render_records(records)
            await pilot.pause()
            seen.append(_widgets(scr.query_one("#trace")))
            lines.append(_turn_lines(scr.query_one("#trace")))
    assert seen[0] == seen[1]
    assert seen[0]
    assert lines == [0, 1]


@pytest.mark.asyncio
async def test_live_feed_skips_record_of_unrendered_reply(tmp_path):
    """Хвост: запись, чьей реплики нет в ленте (сирота), строки не даёт."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system=_SYS,
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        record = {
            "type": "turn_outcome",
            "after": "m1",
            "outcome": _outcome("m_gone").to_payload(),
        }
        scr._render_records([record], live=True)
        await pilot.pause()
        assert _turn_lines(scr.query_one("#trace")) == 0


def _preview_app(segments: list[dict]):
    """Приложение с одним превью сегментов (`render_segments_readonly`)."""
    from gc_tui.services.trace_render import render_segments_readonly
    from textual.app import App
    from textual.containers import VerticalScroll

    class _A(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            render_segments_readonly(self.query_one("#t"), segments)

    return _A()


@pytest.mark.asyncio
async def test_preview_ignores_record(tmp_path):
    pytest.importorskip("textual")
    seen, lines = [], []
    for with_record in (False, True):
        root = tmp_path / str(with_record)
        _records(root, with_record=with_record)
        segments = FileTaskStore(str(root), system=_SYS).read_segments(_TID)
        async with _preview_app(segments).run_test() as pilot:
            await pilot.pause()
            seen.append(_widgets(pilot.app.query_one("#t")))
            lines.append(_turn_lines(pilot.app.query_one("#t")))
    assert seen[0] == seen[1]
    assert seen[0]
    assert lines == [0, 1]
