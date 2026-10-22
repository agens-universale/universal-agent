"""Читатели треда TUI и запись итога хода (`type: "turn_outcome"`).

Запись встаёт в `data_*.jsonl` за последним сообщением хода и повторяет
его `seq`. Читатели сообщений её как сообщение не видят: хвост ленты не
сбрасывается, сжатие остаётся сжатием, счёт сообщений сегмента и экспорта
— только сообщения, лента и превью — те же виджеты, что без записи.
"""

from __future__ import annotations

import json
import os

import pytest
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_DIALOG_SUMMARY,
    wrap_technical_user_content,
)
from gc_core.persistence.turn_outcome import TurnOutcome
from gc_core.thread_store import (
    append_new_thread_messages,
    append_turn_outcome_record,
    archive_and_replace_thread_files,
    load_thread_meta,
    save_thread_meta,
)
from gc_testkit.tui_support import tui_settings
from gc_tui.services.export import export_thread
from gc_tui.services.stores.file_store import FileTaskStore, _payload_ids

_SYS = "cli"
_TID = "t1"
_OUTCOME = TurnOutcome(
    status="done",
    reason=None,
    opened_by="m1",
    started_at="2026-10-22T11:32:00.000Z",
    ended_at="2026-10-22T11:32:12.000Z",
    duration_sec=12.0,
    error=None,
)


def _user(mid: str, text: str = "вопрос") -> dict:
    return {"role": "user", "content": f"{mid} {text}", "id": mid}


def _assistant(mid: str, text: str = "ответ") -> dict:
    return {"role": "assistant", "content": text, "id": mid}


class _Thread:
    def __init__(self, tmp_path) -> None:
        self.dir = str(tmp_path)
        self.seq = 0
        save_thread_meta(self.dir, _SYS, _TID, {})

    def append(self, *msgs: dict) -> None:
        append_new_thread_messages(
            self.dir, _SYS, _TID, list(msgs), set(), start_seq=self.seq
        )
        self.seq += len(msgs)

    def outcome(self) -> None:
        append_turn_outcome_record(
            self.dir, _SYS, _TID, _OUTCOME, seq=self.seq
        )

    def compact(self, suffix: str) -> None:
        meta = load_thread_meta(self.dir, _SYS, _TID)
        summary = {
            "role": "user",
            "content": wrap_technical_user_content(
                "Summarized context from previous dialogue.\nкратко",
                technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY,
            ),
            "id": f"sum_{suffix}",
        }
        archive_and_replace_thread_files(
            self.dir, _SYS, _TID, [summary], archive_suffix=suffix
        )
        save_thread_meta(
            self.dir,
            _SYS,
            _TID,
            {**meta, "last_summary_archive_suffix": suffix},
        )
        self.seq = 1

    @property
    def live(self) -> str:
        return os.path.join(self.dir, f"data_{_SYS}_{_TID}.jsonl")


def _store(thread: _Thread) -> FileTaskStore:
    return FileTaskStore(thread.dir, system=_SYS)


def _ids(records: list[dict]) -> list:
    return [(r.get("payload") or {}).get("id", r.get("type")) for r in records]


# ---- хвост ----------------------------------------------------------------


def test_tail_record_with_equal_seq_is_continuation(tmp_path):
    thread = _Thread(tmp_path)
    thread.append(*[_user(f"m{i}") for i in range(1, 6)])
    store = _store(thread)
    _, cur = store.read_all_events(_TID)

    thread.outcome()
    tail = store.tail_events(_TID, cur)

    assert tail.stream_reset is False
    assert _ids(tail.new_records) == ["turn_outcome"]
    assert tail.new_records[0]["seq"] == 5
    cur = tail.next_cursor
    raw = open(thread.live, "rb").read()
    assert store._on_record_boundary(raw, cur) is True

    thread.append(_user("m6"))
    tail = store.tail_events(_TID, cur)
    assert tail.stream_reset is False
    assert _ids(tail.new_records) == ["m6"]


def test_compaction_after_record_is_a_step_not_reset(tmp_path):
    thread = _Thread(tmp_path)
    thread.append(_user("m1"), _assistant("m2"))
    thread.outcome()
    store = _store(thread)
    _, cur = store.read_all_events(_TID)

    thread.compact("S")
    tail = store.tail_events(_TID, cur)

    assert tail.stream_reset is False
    assert tail.compaction is not None
    assert tail.compaction.compacted_by["messages_in_segment"] == 2


def test_segment_counts_only_messages(tmp_path):
    thread = _Thread(tmp_path)
    thread.append(_user("m1"), _assistant("m2"))
    thread.outcome()
    thread.compact("S")
    thread.append(_user("m3"))

    segments = _store(thread).read_segments(_TID)

    assert segments[0]["compacted_by"]["messages_in_segment"] == 2
    assert _ids(segments[0]["messages"]) == ["m1", "m2", "turn_outcome"]


def test_payload_ids_ignore_record(tmp_path):
    thread = _Thread(tmp_path)
    thread.append(_user("m1"))
    thread.outcome()
    records, _ = _store(thread).read_all_events(_TID)

    assert _payload_ids(records) == frozenset({"m1"})


# ---- экспорт --------------------------------------------------------------


def _export(thread: _Thread, fmt: str) -> str:
    store = _store(thread)
    return export_thread(
        store.read_segments(_TID),
        fmt=fmt,
        meta={},
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
    return [(type(w).__name__, _text(w)) for w in root.query("*")]


@pytest.mark.asyncio
async def test_live_feed_ignores_record(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    seen = []
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
    assert seen[0] == seen[1]
    assert seen[0]


@pytest.mark.asyncio
async def test_preview_ignores_record(tmp_path):
    pytest.importorskip("textual")
    from textual.app import App
    from textual.containers import VerticalScroll

    from gc_tui.services.trace_render import render_segments_readonly

    seen = []
    for with_record in (False, True):
        root = tmp_path / str(with_record)
        _records(root, with_record=with_record)
        segments = FileTaskStore(str(root), system=_SYS).read_segments(_TID)

        class _A(App):
            def compose(self):
                yield VerticalScroll(id="t")

            def on_mount(self):
                render_segments_readonly(self.query_one("#t"), segments)

        async with _A().run_test() as pilot:
            await pilot.pause()
            seen.append(_widgets(pilot.app.query_one("#t")))
    assert seen[0] == seen[1]
    assert seen[0]
