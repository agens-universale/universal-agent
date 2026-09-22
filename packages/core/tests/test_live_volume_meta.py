"""meta.tui.live_history_chars / live_msg_count — агрегаты объёма live (persist)."""

from gc_core.message_format import calc_history_chars_for_trigger
from gc_core.thread_store import (
    ThreadSession,
    discard_message_by_id,
    load_thread_messages,
    load_thread_meta,
)


def _session(tmp_path, tid="t1", *, strip_think=True):
    return ThreadSession(
        threads_dir=str(tmp_path),
        system_id="cli",
        thread_id=tid,
        history_messages=[],
        known_message_keys=set(),
        persisted_messages_count=0,
        meta={},
        previous_response_id=None,
        backend_api_mode="chat_completions",
        strip_think=strip_think,
    )


def _live_tui(tmp_path, tid="t1"):
    """Обратная совместимость имени: live_* теперь в meta.runtime."""
    return load_thread_meta(str(tmp_path), "cli", tid).get("runtime") or {}


async def test_persist_batch_sets_live_volume_matching_full_recompute(tmp_path):
    msgs = [
        {"role": "user", "content": "привет", "id": "m1"},
        {"role": "assistant", "content": "ответ длиннее", "id": "m2"},
    ]
    ts = _session(tmp_path)
    await ts.apersist_batch(msgs, previous_response_id=None)

    tui = _live_tui(tmp_path)
    live, _, _ = load_thread_messages(str(tmp_path), "cli", "t1")
    assert tui["live_msg_count"] == len(live) == 2
    assert tui["live_history_chars"] == calc_history_chars_for_trigger(live)


async def test_persist_batch_increments_on_append(tmp_path):
    ts = _session(tmp_path)
    await ts.apersist_batch(
        [{"role": "user", "content": "a", "id": "m1"}],
        previous_response_id=None,
    )
    await ts.apersist_batch(
        [{"role": "user", "content": "bb", "id": "m2"}],
        previous_response_id=None,
    )

    tui = _live_tui(tmp_path)
    live, _, _ = load_thread_messages(str(tmp_path), "cli", "t1")
    assert tui["live_msg_count"] == 2
    assert tui["live_history_chars"] == calc_history_chars_for_trigger(live)


async def test_compact_resets_live_volume_to_new_live(tmp_path):
    ts = _session(tmp_path)
    await ts.apersist_batch(
        [
            {"role": "user", "content": "длинное " * 20, "id": "m1"},
            {"role": "assistant", "content": "ещё длиннее " * 30, "id": "m2"},
        ],
        previous_response_id=None,
    )
    before = _live_tui(tmp_path)["live_history_chars"]
    assert before > 0

    await ts.aarchive_and_compact_summary_payload({"summary_message_content": "кратко", "dialog_steps_offset": 0})

    tui = _live_tui(tmp_path)
    live, _, _ = load_thread_messages(str(tmp_path), "cli", "t1")
    assert len(live) == 1
    assert tui["live_msg_count"] == 1
    assert tui["live_history_chars"] == calc_history_chars_for_trigger(live)
    assert tui["live_history_chars"] < before


async def test_discard_keeps_live_volume_consistent(tmp_path):
    ts = _session(tmp_path)
    await ts.apersist_batch(
        [
            {"role": "user", "content": "удали меня", "id": "m_orphan"},
            {"role": "user", "content": "останусь", "id": "m_keep"},
        ],
        previous_response_id=None,
    )
    assert discard_message_by_id(str(tmp_path), "cli", "t1", "m_orphan") is True

    tui = _live_tui(tmp_path)
    live, _, _ = load_thread_messages(str(tmp_path), "cli", "t1")
    assert [m["id"] for m in live] == ["m_keep"]
    assert tui["live_msg_count"] == 1
    assert tui["live_history_chars"] == calc_history_chars_for_trigger(live)


async def test_discard_uses_projection_for_live_volume(tmp_path):
    dirty = "<think>hidden</think>visible"
    ts = _session(tmp_path)
    await ts.apersist_batch(
        [
            {"role": "assistant", "content": dirty, "id": "m_keep"},
            {"role": "assistant", "content": "drop", "id": "m_orphan"},
        ],
        previous_response_id=None,
    )
    assert discard_message_by_id(str(tmp_path), "cli", "t1", "m_orphan", strip_think=False) is True
    tui = _live_tui(tmp_path)
    live, _, _ = load_thread_messages(str(tmp_path), "cli", "t1")
    assert [m["id"] for m in live] == ["m_keep"]
    assert tui["live_history_chars"] == calc_history_chars_for_trigger(live, strip_think=False)
    assert tui["live_history_chars"] > calc_history_chars_for_trigger(live)


async def test_append_uses_projection_for_live_volume(tmp_path):
    """Счётчик живого объёма считает ПРОЕКЦИЮ, а не сырой текст.

    Пишем через `ThreadSession`, а не через модульный `append_new_thread_messages`:
    бамп счётчика живёт в сессии, одинаково для любого хранилища. Пока он был в
    файловом слое, у встраивающего с базой ключей `live_*` не появлялось вовсе,
    а TUI их читает.
    """
    dirty = "<think>hidden</think>visible"
    ts = _session(tmp_path, strip_think=False)
    await ts.apersist_batch(
        [{"role": "assistant", "content": "keep", "id": "m1"}],
        previous_response_id=None,
    )
    await ts.apersist_batch(
        [{"role": "assistant", "content": dirty, "id": "m2"}],
        previous_response_id=None,
    )
    tui = _live_tui(tmp_path)
    live, _, _ = load_thread_messages(str(tmp_path), "cli", "t1")
    assert tui["live_history_chars"] == calc_history_chars_for_trigger(live, strip_think=False)
    default_chars = calc_history_chars_for_trigger(live)
    assert tui["live_history_chars"] > default_chars


async def test_persist_batch_preserves_tui_owned_keys(tmp_path):
    """live_* пишет persist в runtime; last_compact_msg_count (TUI) не затирается."""
    from gc_core.thread_store import load_thread_meta, update_tui_meta

    ts = _session(tmp_path)
    await ts.apersist_batch(
        [{"role": "user", "content": "a", "id": "m1"}],
        previous_response_id=None,
    )
    update_tui_meta(str(tmp_path), "cli", "t1", {"last_compact_msg_count": 1})
    await ts.apersist_batch(
        [{"role": "user", "content": "b", "id": "m2"}],
        previous_response_id=None,
    )

    meta = load_thread_meta(str(tmp_path), "cli", "t1")
    assert (meta.get("tui") or {})["last_compact_msg_count"] == 1
    assert (meta.get("runtime") or {})["live_msg_count"] == 2


async def test_compact_preserves_tui_owned_keys(tmp_path):
    from gc_core.thread_store import load_thread_meta, update_tui_meta

    ts = _session(tmp_path)
    await ts.apersist_batch(
        [{"role": "user", "content": "длинное " * 20, "id": "m1"}],
        previous_response_id=None,
    )
    update_tui_meta(
        str(tmp_path),
        "cli",
        "t1",
        {"last_compact_msg_count": 1, "pending_user_messages": [{"id": "p1"}]},
    )
    # self.meta устарел относительно диска — compact не должен затереть disk tui.
    ts.meta = dict(ts.meta)
    ts.meta.pop("tui", None)

    await ts.aarchive_and_compact_summary_payload({"summary_message_content": "кратко", "dialog_steps_offset": 0})

    meta = load_thread_meta(str(tmp_path), "cli", "t1")
    tui = meta.get("tui") or {}
    assert tui["last_compact_msg_count"] == 1
    assert tui["pending_user_messages"] == [{"id": "p1"}]
    assert (meta.get("runtime") or {})["live_msg_count"] == 1
