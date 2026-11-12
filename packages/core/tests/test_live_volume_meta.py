"""runtime.live_history_chars / live_msg_count — агрегаты объёма живой
истории: пишет сессия при шаге и сжатии, фасад — при откате реплики.

Сохранность секции TUI при шаге и сжатии — ``threads/test_session_v2.py``.
"""

from gc_core.message_format import calc_history_chars_for_trigger
from gc_core.persistence.live_volume import live_volume_from_messages
from gc_core.thread_store import ThreadSession
from gc_core.threads.facade import Threads
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey

KEY = ThreadKey(system="cli", thread="t1")


async def _session(tmp_path, *, strip_think=True):
    ts = await ThreadSession.afrom_config(
        settings=None,
        system_id=KEY.system,
        thread_id=KEY.thread,
        store=FileThreadStore(tmp_path),
    )
    ts._proj_strip_think = strip_think
    return ts


async def _runtime(tmp_path):
    return (await FileThreadStore(tmp_path).get_thread(KEY)).runtime


async def _live(tmp_path):
    entries = await FileThreadStore(tmp_path).read_stream(KEY, "messages")
    return [entry.data for entry in entries]


async def _discard(tmp_path, message_id, *, strip_think=True):
    """Откат осиротевшей реплики — как его делает TUI."""

    def recount(messages):
        return live_volume_from_messages(messages, strip_think=strip_think)

    return await Threads(FileThreadStore(tmp_path)).discard_message(
        KEY, message_id, recount=recount
    )


async def test_persist_batch_sets_live_volume_matching_full_recompute(
    tmp_path,
):
    msgs = [
        {"role": "user", "content": "привет", "id": "m1"},
        {"role": "assistant", "content": "ответ длиннее", "id": "m2"},
    ]
    ts = await _session(tmp_path)
    await ts.apersist_batch(msgs, previous_response_id=None)

    runtime = await _runtime(tmp_path)
    live = await _live(tmp_path)
    assert runtime.live_msg_count == len(live) == 2
    assert runtime.live_history_chars == calc_history_chars_for_trigger(live)


async def test_persist_batch_increments_on_append(tmp_path):
    ts = await _session(tmp_path)
    await ts.apersist_batch(
        [{"role": "user", "content": "a", "id": "m1"}],
        previous_response_id=None,
    )
    await ts.apersist_batch(
        [{"role": "user", "content": "bb", "id": "m2"}],
        previous_response_id=None,
    )

    runtime = await _runtime(tmp_path)
    live = await _live(tmp_path)
    assert runtime.live_msg_count == 2
    assert runtime.live_history_chars == calc_history_chars_for_trigger(live)


async def test_compact_resets_live_volume_to_new_live(tmp_path):
    ts = await _session(tmp_path)
    await ts.apersist_batch(
        [
            {"role": "user", "content": "длинное " * 20, "id": "m1"},
            {"role": "assistant", "content": "ещё длиннее " * 30, "id": "m2"},
        ],
        previous_response_id=None,
    )
    before = (await _runtime(tmp_path)).live_history_chars
    assert before > 0

    await ts.aarchive_and_compact_summary_payload(
        {"summary_message_content": "кратко", "dialog_steps_offset": 0}
    )

    runtime = await _runtime(tmp_path)
    live = await _live(tmp_path)
    assert len(live) == 1
    assert runtime.live_msg_count == 1
    assert runtime.live_history_chars == calc_history_chars_for_trigger(live)
    assert runtime.live_history_chars < before


async def test_discard_keeps_live_volume_consistent(tmp_path):
    ts = await _session(tmp_path)
    await ts.apersist_batch(
        [
            {"role": "user", "content": "удали меня", "id": "m_orphan"},
            {"role": "user", "content": "останусь", "id": "m_keep"},
        ],
        previous_response_id=None,
    )
    await ts.arelease_lease()
    assert await _discard(tmp_path, "m_orphan") is True

    runtime = await _runtime(tmp_path)
    live = await _live(tmp_path)
    assert [m["id"] for m in live] == ["m_keep"]
    assert runtime.live_msg_count == 1
    assert runtime.live_history_chars == calc_history_chars_for_trigger(live)


async def test_discard_uses_projection_for_live_volume(tmp_path):
    dirty = "<think>hidden</think>visible"
    ts = await _session(tmp_path)
    await ts.apersist_batch(
        [
            {"role": "assistant", "content": dirty, "id": "m_keep"},
            {"role": "assistant", "content": "drop", "id": "m_orphan"},
        ],
        previous_response_id=None,
    )
    await ts.arelease_lease()
    assert await _discard(tmp_path, "m_orphan", strip_think=False) is True
    runtime = await _runtime(tmp_path)
    live = await _live(tmp_path)
    assert [m["id"] for m in live] == ["m_keep"]
    assert runtime.live_history_chars == calc_history_chars_for_trigger(
        live, strip_think=False
    )
    assert runtime.live_history_chars > calc_history_chars_for_trigger(live)


async def test_append_uses_projection_for_live_volume(tmp_path):
    """Счётчик живого объёма считает ПРОЕКЦИЮ, а не сырой текст.

    Бамп счётчика живёт в сессии, одинаково для любого хранилища: пока он
    был в файловом слое, у встраивающего с базой ключей ``live_*`` не
    появлялось вовсе, а TUI их читает.
    """
    dirty = "<think>hidden</think>visible"
    ts = await _session(tmp_path, strip_think=False)
    await ts.apersist_batch(
        [{"role": "assistant", "content": "keep", "id": "m1"}],
        previous_response_id=None,
    )
    await ts.apersist_batch(
        [{"role": "assistant", "content": dirty, "id": "m2"}],
        previous_response_id=None,
    )
    runtime = await _runtime(tmp_path)
    live = await _live(tmp_path)
    assert runtime.live_history_chars == calc_history_chars_for_trigger(
        live, strip_think=False
    )
    assert runtime.live_history_chars > calc_history_chars_for_trigger(live)
