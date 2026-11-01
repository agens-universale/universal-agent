"""Мысль модели — поток ``reasoning`` хранилища v2: привязка к последнему
записанному сообщению, вне истории."""

from gc_core.persistence.reasoning_record import ReasoningRecord
from gc_core.thread_store import ThreadSession
from gc_core.threads.facade import Threads
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_testkit.settings import make_settings

KEY = ThreadKey(system="cli", thread="t")
REC = ReasoningRecord(text="план", duration_sec=2.5, attempt="att_1")


def _msg(mid, role):
    return {"id": mid, "role": role, "content": f"{role} {mid}"}


async def _session(store):
    return await ThreadSession.afrom_config(
        settings=make_settings({}),
        system_id="cli",
        thread_id="t",
        store=store,
    )


async def test_append_reasoning_lands_after_last_message(tmp_path):
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)
    await session.apersist_batch(
        [_msg("msg_1", "user")], previous_response_id=None
    )

    assert await session.aappend_reasoning(REC, discarded=False) is True

    entries = await Threads(store).reasoning(KEY)
    assert [e.data for e in entries] == [
        {"after": "msg_1", "thought": REC.to_payload()}
    ]
    ids = [e.data["id"] for e in await store.read_stream(KEY, "messages")]
    assert ids == ["msg_1"]


async def test_empty_thread_gets_no_record(tmp_path):
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)

    assert await session.aappend_reasoning(REC, discarded=False) is False
    assert await Threads(store).reasoning(KEY) == []


async def test_discarded_step_gets_no_thought(tmp_path):
    """Шаг отброшен стопом: мысль его не пишется, отложенное не сливается;
    без отброса — сливается, мысль за ним."""
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)
    await session.apersist_batch(
        [_msg("u1", "user")], previous_response_id=None
    )
    session._defer_persist = True
    await session.apersist_batch(
        [_msg("a1", "assistant")], previous_response_id=None
    )

    assert await session.aappend_reasoning(REC, discarded=True) is False
    ids = [e.data["id"] for e in await store.read_stream(KEY, "messages")]
    assert ids == ["u1"]
    assert await Threads(store).reasoning(KEY) == []
    assert await session.aappend_reasoning(REC, discarded=False) is True
    ids = [e.data["id"] for e in await store.read_stream(KEY, "messages")]
    assert ids == ["u1", "a1"]
    entries = await Threads(store).reasoning(KEY)
    assert [e.data["after"] for e in entries] == ["a1"]
