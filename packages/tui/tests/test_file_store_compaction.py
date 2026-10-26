"""tail_events опознаёт сжатие по новому архиву (спека 3.1).

Шаги ядра воспроизводятся по одному настоящими функциями thread_store:
1 — data в архив, 2 — meta в архив, 3 — новый data со сводкой,
4 — новая meta с эпохой, 5 — дописка сохранённых сообщений.
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_DIALOG_SUMMARY,
    wrap_technical_user_content,
)
from gc_core.thread_store import (
    append_new_thread_messages,
    archive_and_replace_thread_files,
    delete_thread,
    load_thread_meta,
    save_thread_meta,
)
from gc_tui.models import TaskCursor
from gc_tui.services.stores.file_store import FileTaskStore

_SYS = "cli"
_TID = "t1"
_BODY = "x" * 200


def _msg(mid: str, body: str = _BODY) -> dict:
    return {"role": "user", "content": f"{mid} {body}", "id": mid}


def _summary(mid: str, text: str = "кратко") -> dict:
    return {
        "role": "user",
        "content": wrap_technical_user_content(
            f"Summarized context from previous dialogue.\n{text}",
            technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY,
        ),
        "id": mid,
    }


class _Thread:
    """Тред на диске: запись сообщений и шаги сжатия ядра по одному."""

    def __init__(self, tmp_path) -> None:
        self.dir = str(tmp_path)
        self.seq = 0
        self.meta: dict = {}
        save_thread_meta(self.dir, _SYS, _TID, {})

    def _paths(self) -> tuple[str, str]:
        return (
            os.path.join(self.dir, f"data_{_SYS}_{_TID}.jsonl"),
            os.path.join(self.dir, f"meta_{_SYS}_{_TID}.json"),
        )

    def append(self, *msgs: dict) -> None:
        append_new_thread_messages(
            self.dir, _SYS, _TID, list(msgs), set(), start_seq=self.seq
        )
        self.seq += len(msgs)

    def step1(self, suffix: str) -> None:
        self.meta = load_thread_meta(self.dir, _SYS, _TID)
        data, _ = self._paths()
        os.replace(
            data, data.replace(".jsonl", f"__pre_summary__{suffix}.jsonl")
        )

    def step2(self, suffix: str) -> None:
        _, meta = self._paths()
        os.replace(
            meta, meta.replace(".json", f"__pre_summary__{suffix}.json")
        )

    def step3(self, suffix: str, summary: dict) -> None:
        archive_and_replace_thread_files(
            self.dir, _SYS, _TID, [summary], archive_suffix=suffix
        )
        self.seq = 1

    def step4(self, suffix: str) -> None:
        save_thread_meta(
            self.dir,
            _SYS,
            _TID,
            {**self.meta, "last_summary_archive_suffix": suffix},
        )

    def step5(self, *preserved: dict) -> None:
        self.append(*preserved)

    def compact(
        self, suffix: str, *, upto: int = 4, summary: dict | None = None
    ) -> None:
        steps = [
            lambda: self.step1(suffix),
            lambda: self.step2(suffix),
            lambda: self.step3(suffix, summary or _summary(f"sum_{suffix}")),
            lambda: self.step4(suffix),
        ]
        for step in steps[:upto]:
            step()


def _ids(records: list[dict]) -> list[str]:
    return [r["payload"]["id"] for r in records]


def _store(thread: _Thread) -> FileTaskStore:
    return FileTaskStore(thread.dir, system=_SYS)


def _attached(tmp_path) -> tuple[_Thread, FileTaskStore, TaskCursor]:
    """Тред m1..m3, курсор после них, m4 дописан после курсора."""
    thread = _Thread(tmp_path)
    thread.append(_msg("m1"), _msg("m2"), _msg("m3"))
    store = _store(thread)
    _, cur = store.read_all_events(_TID)
    thread.append(_msg("m4"))
    return thread, store, cur


def test_compaction_step_after_meta_written(tmp_path):
    thread, store, cur = _attached(tmp_path)
    thread.compact("S", upto=4)

    tail = store.tail_events(_TID, cur)

    assert tail.stream_reset is False
    assert tail.new_records == []
    step = tail.compaction
    assert _ids(step.archive_tail) == ["m4"]
    assert step.segment_index == 0
    assert step.compacted_by == store.read_segments(_TID)[0]["compacted_by"]
    assert step.window_ids == frozenset({"m1", "m2", "m3", "m4"})
    assert step.live_records == []
    live = tmp_path / f"data_{_SYS}_{_TID}.jsonl"
    assert tail.next_cursor == TaskCursor(
        position_token=live.stat().st_size,
        ordering_key=1,
        storage_epoch="S",
        archives=1,
        wait_ticks=0,
    )


def test_compaction_window_ids_cover_archive_and_live(tmp_path):
    thread, store, cur = _attached(tmp_path)
    thread.compact("S", upto=4)
    thread.step5(_msg("m4"), _msg("m5"))

    step = store.tail_events(_TID, cur).compaction

    assert step.window_ids == frozenset({"m1", "m2", "m3", "m4", "m5"})


def test_compaction_live_records_without_leading_summary(tmp_path):
    thread, store, cur = _attached(tmp_path)
    thread.compact("S", upto=4)
    thread.step5(_msg("m3"), _msg("m4"))

    tail = store.tail_events(_TID, cur)

    step = tail.compaction
    assert _ids(step.live_records) == ["m3", "m4"]
    assert tail.next_cursor.ordering_key == 3


def test_compaction_cursor_stops_before_unterminated_line(tmp_path):
    thread, store, cur = _attached(tmp_path)
    thread.compact("S", upto=4)
    live = tmp_path / f"data_{_SYS}_{_TID}.jsonl"
    complete = live.stat().st_size
    with open(live, "a", encoding="utf-8") as f:
        f.write('{"type": "message", "seq": 2, "payl')

    tail = store.tail_events(_TID, cur)

    assert tail.compaction.live_records == []
    assert tail.next_cursor.position_token == complete


@pytest.mark.parametrize(
    "stage", ["after_step1", "after_step2", "after_step3"]
)
def test_wait_during_compaction_steps(tmp_path, stage):
    thread, store, cur = _attached(tmp_path)
    thread.compact("S", upto=int(stage[-1]))

    tail = store.tail_events(_TID, cur)

    assert tail.stream_reset is False and tail.compaction is None
    assert tail.new_records == []
    assert tail.next_cursor.position_token == cur.position_token
    # На первом сжатии шаги 1-2 идут нынешним путём «data-файла нет»
    # (E и C оба None) — счётчик растёт только после шага 3.
    if stage == "after_step3":
        assert tail.next_cursor.wait_ticks == 1


def test_wait_when_meta_missing_on_second_compaction(tmp_path):
    thread, store, _ = _attached(tmp_path)
    thread.compact("S1")
    _, cur = store.read_all_events(_TID)
    thread.compact("S2", upto=2)

    tail = store.tail_events(_TID, cur)

    assert tail.stream_reset is False and tail.compaction is None
    assert tail.next_cursor == replace(cur, wait_ticks=1)


def test_wait_bounded_then_reset(tmp_path):
    thread, store, cur = _attached(tmp_path)
    thread.compact("S", upto=3)

    for _ in range(8):
        tail = store.tail_events(_TID, cur)
        cur = tail.next_cursor
        assert tail.stream_reset is False

    assert store.tail_events(_TID, cur).stream_reset is True


def test_plain_tick_keeps_archives_and_resets_wait(tmp_path):
    thread = _Thread(tmp_path)
    thread.append(_msg("m1"))
    store = _store(thread)
    _, cur = store.read_all_events(_TID)
    cur = replace(cur, archives=1, wait_ticks=3)
    thread.append(_msg("m2"))

    tail = store.tail_events(_TID, cur)

    assert _ids(tail.new_records) == ["m2"]
    assert tail.next_cursor.archives == 1 and tail.next_cursor.wait_ticks == 0


def test_second_compaction_is_a_step_not_reset(tmp_path):
    thread, store, cur = _attached(tmp_path)
    thread.compact("S1")
    thread.step5(_msg("m4"))
    first = store.tail_events(_TID, cur)
    assert first.compaction.segment_index == 0
    cur = first.next_cursor
    thread.append(_msg("m5"))
    plain = store.tail_events(_TID, cur)
    assert _ids(plain.new_records) == ["m5"]
    cur = plain.next_cursor
    thread.append(_msg("m6"))
    thread.compact("S2", upto=4)

    tail = store.tail_events(_TID, cur)

    assert tail.stream_reset is False
    step = tail.compaction
    assert step.segment_index == 1
    assert _ids(step.archive_tail) == ["m6"]
    assert step.compacted_by == store.read_segments(_TID)[1]["compacted_by"]
    assert step.window_ids == frozenset({"sum_S1", "m4", "m5", "m6"})
    assert tail.next_cursor.archives == 2


def test_first_compaction_long_summary_waits_then_steps(tmp_path):
    thread = _Thread(tmp_path)
    big = "y" * 1000
    thread.append(_msg("m1", big), _msg("m2", big), _msg("m3", big))
    store = _store(thread)
    _, cur = store.read_all_events(_TID)
    # Эпохи ещё нет, сводка длиннее смещения курсора: укорочения не видно,
    # курсор посреди строки нового файла — это ожидание шага, не хвост.
    thread.compact("S", upto=3, summary=_summary("sum", "s" * 4000))
    tick = store.tail_events(_TID, cur)
    assert tick.stream_reset is False and tick.new_records == []
    assert tick.next_cursor.position_token == cur.position_token
    thread.step4("S")

    tail = store.tail_events(_TID, tick.next_cursor)

    assert tail.stream_reset is False
    assert tail.compaction.archive_tail == []


def test_archive_tail_from_zero_drops_leading_summary(tmp_path):
    thread, store, _ = _attached(tmp_path)
    thread.compact("S1")
    thread.step5(_msg("m4"))
    cur = TaskCursor(0, 0, "S1", archives=1)
    thread.compact("S2", upto=4)

    tail = store.tail_events(_TID, cur)

    assert _ids(tail.compaction.archive_tail) == ["m4"]
    assert tail.compaction.segment_index == 1


def test_truncation_without_new_archive_resets(tmp_path):
    thread, store, cur = _attached(tmp_path)
    live = tmp_path / f"data_{_SYS}_{_TID}.jsonl"
    live.write_text(live.read_text(encoding="utf-8").splitlines()[0] + "\n")

    tail = store.tail_events(_TID, cur)

    assert tail.stream_reset is True
    assert tail.reason == "file truncated"


def test_thread_deleted_resets_not_waits(tmp_path):
    thread, store, _ = _attached(tmp_path)
    thread.compact("S1")
    _, cur = store.read_all_events(_TID)
    delete_thread(thread.dir, _SYS, _TID)

    tail = store.tail_events(_TID, cur)

    assert tail.stream_reset is True


def test_two_new_archives_reset(tmp_path):
    thread, store, cur = _attached(tmp_path)
    thread.compact("S1")
    thread.compact("S2")

    assert store.tail_events(_TID, cur).stream_reset is True


def test_cursor_beyond_archive_end_resets(tmp_path):
    thread, store, cur = _attached(tmp_path)
    cur = replace(cur, position_token=cur.position_token + 10_000)
    thread.compact("S", upto=4)

    tail = store.tail_events(_TID, cur)

    assert tail.stream_reset is True and tail.compaction is None


def test_attach_mid_compaction_resets_once(tmp_path):
    thread, store, _ = _attached(tmp_path)
    thread.compact("S", upto=2)
    _, cur = store.read_all_events(_TID)
    thread.step3("S", _summary("sum"))
    thread.step4("S")

    assert store.tail_events(_TID, cur).stream_reset is True

    _, cur = store.read_all_events(_TID)
    thread.step5(_msg("m4"))
    tail = store.tail_events(_TID, cur)

    assert tail.stream_reset is False and tail.compaction is None
    assert _ids(tail.new_records) == ["m4"]
    assert tail.next_cursor.wait_ticks == 0


def test_attach_during_compaction_reads_consistent_cursor(
    tmp_path, monkeypatch
):
    """Сжатие прошло, пока attach читал живой файл: прочитан старый файл,
    а эпоха и архивы уже новые — файл перечитывается, курсор согласован."""
    thread, store, _ = _attached(tmp_path)
    read_live = FileTaskStore._read_live

    def _compact_after_read(self, task_id, system):
        monkeypatch.setattr(FileTaskStore, "_read_live", read_live)
        raw = read_live(self, task_id, system)
        thread.compact("S", upto=4)
        return raw

    monkeypatch.setattr(FileTaskStore, "_read_live", _compact_after_read)
    records, cur = store.read_all_events(_TID)
    tail = store.tail_events(_TID, cur)

    assert _ids(records) == ["sum_S"]
    assert tail.stream_reset is False and tail.compaction is None
    assert cur.storage_epoch == "S" and cur.archives == 1


def test_attach_snapshot_settles_after_three_compaction_changes(
    tmp_path, monkeypatch
):
    """Второе сжатие меняет эпоху и архивы трижды, по разу на каждое
    чтение живого файла: наружу всё равно уходит согласованная пара."""
    thread, store, _ = _attached(tmp_path)
    thread.compact("S1")
    read_live = FileTaskStore._read_live

    def _summary_and_meta():
        thread.step3("S2", _summary("sum_S2"))
        thread.step4("S2")

    changes = [
        lambda: thread.step1("S2"),
        lambda: thread.step2("S2"),
        _summary_and_meta,
    ]

    def _change_after_read(self, task_id, system):
        raw = read_live(self, task_id, system)
        if changes:
            changes.pop(0)()
        return raw

    monkeypatch.setattr(FileTaskStore, "_read_live", _change_after_read)
    records, cur = store.read_all_events(_TID)
    tail = store.tail_events(_TID, cur)

    assert _ids(records) == ["sum_S2"]
    assert cur.storage_epoch == "S2" and cur.archives == 2
    assert tail.stream_reset is False and tail.compaction is None


def test_plain_tick_does_not_list_archives(tmp_path, monkeypatch):
    thread = _Thread(tmp_path)
    thread.append(_msg("m1"))
    store = _store(thread)
    _, cur = store.read_all_events(_TID)

    def _boom(*_args, **_kwargs):
        raise AssertionError("archives listed on a plain tick")

    monkeypatch.setattr(FileTaskStore, "_archive_paths", _boom)
    thread.append(_msg("m2"))

    tail = store.tail_events(_TID, cur)

    assert _ids(tail.new_records) == ["m2"]
