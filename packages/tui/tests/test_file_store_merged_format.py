"""Round-trip: FileTaskStore читает тред, записанный настоящим thread_store (merged core).

Тест не мокает формат вручную — он вызывает ядро thread_store для записи,
затем проверяет, что FileTaskStore корректно читает результат.
"""

from __future__ import annotations

from gc_core.thread_store import ThreadSession, save_thread_meta
from gc_tui.services.stores.file_store import FileTaskStore

_SYSTEM = "cli"
_TID = "roundtrip01"


def _make_session(threads_dir: str) -> ThreadSession:
    """Создать ThreadSession с минимальным конфигом."""
    return ThreadSession(
        threads_dir=threads_dir,
        system_id=_SYSTEM,
        thread_id=_TID,
        history_messages=[],
        known_message_keys=set(),
        persisted_messages_count=0,
        meta={},
        previous_response_id=None,
        backend_api_mode="responses",
    )


# ---------------------------------------------------------------------------
# 1. list_tasks — meta-поля
# ---------------------------------------------------------------------------


async def test_file_store_list_tasks_reads_core_meta(tmp_path):
    """list_tasks() понимает тред, записанный сессией ядра."""
    threads_dir = str(tmp_path)
    session = _make_session(threads_dir)
    await session.apersist_batch(
        [{"role": "user", "content": f"m{n}"} for n in range(3)],
        previous_response_id=None,
    )

    store = FileTaskStore(threads_dir=threads_dir, system=_SYSTEM)
    tasks = store.list_tasks()

    assert len(tasks) == 1
    t = tasks[0]
    assert t.thread_id == _TID
    assert t.messages_count == 3
    # ни meta.status, ни meta.tui.status → исход не записан → None (не выдумываем)
    assert t.status is None
    # unread_count отсутствует → 0
    assert t.unread_count == 0
    # last_error отсутствует → None
    assert t.last_error is None
    # skill_id — в meta нет "tui" → None
    assert t.skill_id is None


def test_file_store_list_tasks_reads_token_usage(tmp_path):
    """list_tasks() прокидывает meta.token_usage в TaskSummary."""
    threads_dir = str(tmp_path)
    usage = {
        "by_category": {
            "main": {
                "input": 100,
                "output": 20,
                "cached": 0,
                "calls": 1,
                "missing_usage_calls": 0,
            }
        }
    }
    save_thread_meta(
        threads_dir,
        _SYSTEM,
        _TID,
        {
            "thread_id": _TID,
            "messages_count": 1,
            "token_usage": usage,
        },
    )
    store = FileTaskStore(threads_dir=threads_dir, system=_SYSTEM)
    tasks = store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].token_usage == usage


# ---------------------------------------------------------------------------
# 2. read_all_events — envelope полей (type, seq, payload)
# ---------------------------------------------------------------------------


async def test_file_store_read_all_events_reads_core_data(tmp_path):
    """read_all_events() правильно читает записи ядра: envelope type/seq/payload."""
    threads_dir = str(tmp_path)
    session = _make_session(threads_dir)

    msgs = [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "хай"},
    ]
    await session.apersist_batch(msgs, previous_response_id=None)

    store = FileTaskStore(threads_dir=threads_dir, system=_SYSTEM)
    records, cursor = store.read_all_events(_TID)

    assert len(records) == 2
    # Envelope: поле type — "message"
    assert all(r["type"] == "message" for r in records)
    # Envelope: поле seq — целое, монотонное
    seqs = [r["seq"] for r in records]
    assert seqs == [1, 2]
    # Envelope: поле payload — dict с role/content
    assert records[0]["payload"]["role"] == "user"
    assert records[0]["payload"]["content"] == "привет"
    assert records[1]["payload"]["role"] == "assistant"

    # Cursor: ordering_key == max seq
    assert cursor.ordering_key == 2
    # Обычный шаг эпоху не меняет: показ треда не сбрасывается.
    await session.apersist_batch(
        [{"role": "assistant", "content": "ещё"}], previous_response_id=None
    )
    assert store.read_all_events(_TID)[1].storage_epoch == cursor.storage_epoch


# ---------------------------------------------------------------------------
# 3. stream-reset epoch: сжатие истории меняет эпоху курсора
# ---------------------------------------------------------------------------


async def test_file_store_epoch_changes_with_compaction(tmp_path):
    """Сжатие истории ядром меняет storage_epoch, а хвост отдаёт его шагом
    сжатия: экран дописывает трейс, а не начинает показ заново."""
    threads_dir = str(tmp_path)
    session = _make_session(threads_dir)

    msgs = [{"role": "user", "content": "start"}]
    await session.apersist_batch(msgs, previous_response_id=None)
    store = FileTaskStore(threads_dir=threads_dir, system=_SYSTEM)
    _records, before = store.read_all_events(_TID)

    await session.aarchive_and_compact_summary_payload(
        {
            "summary_message_content": "Диалог начался.",
            "user_intent_log": [],
            "dialog_steps_offset": 0,
        }
    )

    records, after = store.read_all_events(_TID)
    # После компакции живая история начинается со сводки
    assert len(records) >= 1
    assert after.storage_epoch != before.storage_epoch
    tail = store.tail_events(_TID, before)
    assert tail.stream_reset is False
    assert tail.compaction is not None
    assert tail.compaction.segment_index == 0
    assert tail.next_cursor == after


# ---------------------------------------------------------------------------
# 4. read_segments — архивная glob и is_compaction_summary
# ---------------------------------------------------------------------------


async def test_file_store_read_segments_with_core_compaction(tmp_path):
    """read_segments читает сегменты после реальной компакции через archive_and_compact."""
    threads_dir = str(tmp_path)
    session = _make_session(threads_dir)

    # Шаг 1: записываем 2 сообщения, затем компактим
    msgs1 = [
        {"role": "user", "content": "вопрос-0"},
        {"role": "assistant", "content": "ответ-0"},
    ]
    await session.apersist_batch(msgs1, previous_response_id=None)
    await session.aarchive_and_compact_summary_payload(
        {
            "summary_message_content": "СИНТЕЗ-1",
            "user_intent_log": [],
            "dialog_steps_offset": 0,
        }
    )

    # Шаг 2: записываем ещё сообщения
    msgs2 = [{"role": "user", "content": "вопрос-1"}]
    await session.apersist_batch(msgs2, previous_response_id=None)

    store = FileTaskStore(threads_dir=threads_dir, system=_SYSTEM)
    segs = store.read_segments(_TID)

    assert len(segs) == 2
    # seg0 — архив (вопрос-0, ответ-0), сжат → СИНТЕЗ-1
    assert segs[0]["compacted_by"] is not None
    assert "СИНТЕЗ-1" in segs[0]["compacted_by"]["synthesis"]
    assert segs[0]["compacted_by"]["messages_in_segment"] == 2
    # seg1 (live) — вопрос-1, не сжат
    assert segs[1]["compacted_by"] is None
    assert segs[1]["messages"][0]["payload"]["content"] == "вопрос-1"


# ---------------------------------------------------------------------------
# 5. delete_task — удаление треда хранилищем
# ---------------------------------------------------------------------------


async def test_file_store_delete_task_removes_all_files(tmp_path):
    """delete_task удаляет data + meta + pre_summary архивы."""
    threads_dir = str(tmp_path)
    session = _make_session(threads_dir)

    msgs = [{"role": "user", "content": "x"}]
    await session.apersist_batch(msgs, previous_response_id=None)
    await session.aarchive_and_compact_summary_payload(
        {
            "summary_message_content": "sum",
            "user_intent_log": [],
            "dialog_steps_offset": 0,
        }
    )

    # Убеждаемся, что файлы существуют
    files_before = list(tmp_path.glob(f"*{_TID}*"))
    assert len(files_before) >= 3  # data, meta, archive_data, archive_meta

    store = FileTaskStore(threads_dir=threads_dir, system=_SYSTEM)
    store.delete_task(_TID)

    files_after = list(tmp_path.glob(f"*{_TID}*"))
    assert files_after == [], f"Остались файлы: {files_after}"
