"""tail_events опознаёт сжатие и отдаёт его шагом (спека 3.1).

Сжатие пишется, как его пишет ядро: одной записью хранилища
(``SessionPort.aarchive_and_replace``) — история в архив, вместо неё сводка;
сохранённые сообщения дописываются следом обычной записью. Промежуточных
состояний (архив есть, сводки ещё нет) читатель не видит, поэтому и ждать
их тикам не нужно.
"""

from __future__ import annotations

from dataclasses import replace

from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_DIALOG_SUMMARY,
    wrap_technical_user_content,
)
from gc_core.threads.facade import Threads, run_sync
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_core.threads.session_port import SessionPort
from gc_tui.models import TaskCursor
from gc_tui.services.stores.file_store import FileTaskStore

_SYS = "cli"
_TID = "t1"
_KEY = ThreadKey(system=_SYS, thread=_TID)
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
    """Тред на диске; пишет его отдельный экземпляр хранилища — как агент
    рядом с TUI."""

    def __init__(self, tmp_path) -> None:
        self.dir = str(tmp_path)
        self.store = FileThreadStore(tmp_path)
        self.threads = Threads(self.store)

    def append(self, *msgs: dict) -> None:
        run_sync(self.threads.append_messages(_KEY, list(msgs)))

    def compact(self, name: str, *messages: dict) -> None:
        """Сжатие ядра: история в архив, вместо неё ``messages`` (по
        умолчанию — одна сводка ``sum_<name>``)."""
        messages = list(messages) or [_summary(f"sum_{name}")]
        self._as_owner(
            lambda port: port.aarchive_and_replace(_SYS, _TID, messages)
        )

    def replace_without_archive(self, *messages: dict) -> None:
        self._as_owner(
            lambda port: port.areplace_messages(_SYS, _TID, list(messages))
        )

    def _as_owner(self, write) -> None:
        """Запись хода: под арендой треда, которую ход потом отпускает."""

        async def run() -> None:
            port = SessionPort(self.store)
            await write(port)
            await port.arelease_lease(_SYS, _TID)

        run_sync(run())

    def delete(self) -> None:
        run_sync(self.threads.delete(_KEY))


def _ids(records: list[dict]) -> list[str]:
    return [r["payload"]["id"] for r in records]


def _attached(tmp_path) -> tuple[_Thread, FileTaskStore, TaskCursor]:
    """Тред m1..m3, курсор после них, m4 дописан после курсора."""
    thread = _Thread(tmp_path)
    thread.append(_msg("m1"), _msg("m2"), _msg("m3"))
    store = FileTaskStore(thread.dir, system=_SYS)
    _, cur = store.read_all_events(_TID)
    thread.append(_msg("m4"))
    return thread, store, cur


def _at_seq(cur: TaskCursor, seq: int) -> TaskCursor:
    """Курсор той же версии истории, прочитавший её до ``seq``."""
    token = replace(cur.position_token, seq={"messages": seq})
    return replace(cur, position_token=token, ordering_key=seq)


def test_compaction_is_a_step(tmp_path):
    thread, store, cur = _attached(tmp_path)
    thread.compact("S")

    tail = store.tail_events(_TID, cur)

    assert tail.stream_reset is False
    assert tail.new_records == []
    step = tail.compaction
    assert _ids(step.archive_tail) == ["m4"]
    assert step.segment_index == 0
    assert step.compacted_by == store.read_segments(_TID)[0]["compacted_by"]
    assert step.window_ids == frozenset({"m1", "m2", "m3", "m4"})
    assert step.live_records == []
    assert tail.next_cursor.ordering_key == 1
    assert tail.next_cursor.archives == 1
    after = store.tail_events(_TID, tail.next_cursor)
    assert after.compaction is None and after.stream_reset is False
    assert after.new_records == []


def test_compaction_window_ids_cover_archive_and_live(tmp_path):
    thread, store, cur = _attached(tmp_path)
    thread.compact("S")
    thread.append(_msg("m4"), _msg("m5"))

    step = store.tail_events(_TID, cur).compaction

    assert step.window_ids == frozenset({"m1", "m2", "m3", "m4", "m5"})


def test_compaction_live_records_without_leading_summary(tmp_path):
    thread, store, cur = _attached(tmp_path)
    thread.compact("S")
    thread.append(_msg("m3"), _msg("m4"))

    tail = store.tail_events(_TID, cur)

    assert _ids(tail.compaction.live_records) == ["m3", "m4"]
    assert tail.next_cursor.ordering_key == 3


def test_compaction_cursor_stops_before_unterminated_line(tmp_path):
    thread, store, cur = _attached(tmp_path)
    thread.compact("S")
    live = tmp_path / f"data_{_SYS}_{_TID}.jsonl"
    with open(live, "a", encoding="utf-8") as f:
        f.write('{"type": "message", "seq": 2, "payl')

    tail = store.tail_events(_TID, cur)

    assert tail.compaction.live_records == []
    assert tail.next_cursor.ordering_key == 1


def test_plain_tick_keeps_archives_and_resets_wait(tmp_path):
    thread = _Thread(tmp_path)
    thread.append(_msg("m1"))
    store = FileTaskStore(thread.dir, system=_SYS)
    _, cur = store.read_all_events(_TID)
    cur = replace(cur, archives=1, wait_ticks=3)
    thread.append(_msg("m2"))

    tail = store.tail_events(_TID, cur)

    assert _ids(tail.new_records) == ["m2"]
    assert tail.next_cursor.archives == 1 and tail.next_cursor.wait_ticks == 0


def test_second_compaction_is_a_step_not_reset(tmp_path):
    thread, store, cur = _attached(tmp_path)
    thread.compact("S1")
    thread.append(_msg("m4"))
    first = store.tail_events(_TID, cur)
    assert first.compaction.segment_index == 0
    cur = first.next_cursor
    thread.append(_msg("m5"))
    plain = store.tail_events(_TID, cur)
    assert _ids(plain.new_records) == ["m5"]
    cur = plain.next_cursor
    thread.append(_msg("m6"))
    thread.compact("S2")

    tail = store.tail_events(_TID, cur)

    assert tail.stream_reset is False
    step = tail.compaction
    assert step.segment_index == 1
    assert _ids(step.archive_tail) == ["m6"]
    assert step.compacted_by == store.read_segments(_TID)[1]["compacted_by"]
    assert step.window_ids == frozenset({"sum_S1", "m4", "m5", "m6"})
    assert tail.next_cursor.archives == 2


def test_archive_tail_from_zero_drops_leading_summary(tmp_path):
    thread, store, _ = _attached(tmp_path)
    thread.compact("S1")
    thread.append(_msg("m4"))
    _, cur = store.read_all_events(_TID)
    thread.compact("S2")

    tail = store.tail_events(_TID, _at_seq(cur, 0))

    assert _ids(tail.compaction.archive_tail) == ["m4"]
    assert tail.compaction.segment_index == 1


def test_history_replaced_without_archive_resets(tmp_path):
    thread, store, cur = _attached(tmp_path)
    thread.replace_without_archive(_msg("m1"))

    tail = store.tail_events(_TID, cur)

    assert tail.stream_reset is True and tail.compaction is None


def test_new_history_without_summary_resets(tmp_path):
    """Архив есть, но живая история не начинается сводкой — это не
    сжатие."""
    thread, store, cur = _attached(tmp_path)
    thread.compact("S", _msg("n1"))

    tail = store.tail_events(_TID, cur)

    assert tail.stream_reset is True and tail.compaction is None


def test_thread_deleted_resets(tmp_path):
    thread, store, _ = _attached(tmp_path)
    thread.compact("S1")
    _, cur = store.read_all_events(_TID)
    thread.delete()
    thread.append(_msg("n1"))

    tail = store.tail_events(_TID, cur)

    assert tail.stream_reset is True and tail.compaction is None


def test_two_compactions_between_ticks_reset(tmp_path):
    thread, store, cur = _attached(tmp_path)
    thread.compact("S1")
    thread.compact("S2")

    assert store.tail_events(_TID, cur).stream_reset is True


def test_cursor_beyond_archive_end_resets(tmp_path):
    thread, store, cur = _attached(tmp_path)
    cur = _at_seq(cur, 99)
    thread.compact("S")

    tail = store.tail_events(_TID, cur)

    assert tail.stream_reset is True and tail.compaction is None


def test_attach_during_compaction_reads_consistent_cursor(
    tmp_path, monkeypatch
):
    """Сжатие прошло между чтением записи треда и его истории: снимок
    перечитывается, курсор несёт новое число архивов."""
    thread, store, _ = _attached(tmp_path)
    tail = Threads.tail
    calls: list = []

    async def _compact_first(self, key, streams, cursor):
        if not calls:
            calls.append(key)
            port = SessionPort(thread.store)
            await port.aarchive_and_replace(_SYS, _TID, [_summary("sum_S")])
            await port.arelease_lease(_SYS, _TID)
        return await tail(self, key, streams, cursor)

    monkeypatch.setattr(Threads, "tail", _compact_first)
    records, cur = store.read_all_events(_TID)
    monkeypatch.setattr(Threads, "tail", tail)
    after = store.tail_events(_TID, cur)

    assert _ids(records) == ["sum_S"]
    assert cur.archives == 1
    assert after.stream_reset is False and after.compaction is None


def test_plain_tick_does_not_read_archives(tmp_path, monkeypatch):
    thread = _Thread(tmp_path)
    thread.append(_msg("m1"))
    store = FileTaskStore(thread.dir, system=_SYS)
    _, cur = store.read_all_events(_TID)

    def _boom(*_args, **_kwargs):
        raise AssertionError("archive read on a plain tick")

    monkeypatch.setattr(FileThreadStore, "read_archive", _boom)
    thread.append(_msg("m2"))

    tail = store.tail_events(_TID, cur)

    assert _ids(tail.new_records) == ["m2"]
