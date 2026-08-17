"""discard_message_by_id: точечное удаление записи по payload.id (откат
осиротевшей реплики при быстрой отмене). id-идентификация однозначна даже при
повторном вводе того же текста."""
from gc_core.thread_store import ThreadSession, discard_message_by_id, load_thread_messages, save_thread_meta


def _session(tmp_path):
    return ThreadSession(
        threads_dir=str(tmp_path), system_id="cli", thread_id="t1",
        history_messages=[], known_message_keys=set(), persisted_messages_count=0,
        meta={}, previous_response_id=None, backend_api_mode="chat_completions",
    )


def test_removes_only_matching_id(tmp_path):
    ts = _session(tmp_path)
    ts.persist_batch(
        [{"role": "user", "content": "одинаковый", "id": "m_orphan"},
         {"type": "function_call", "call_id": "c1", "name": "x", "arguments": "{}", "id": "a1"},
         {"role": "user", "content": "одинаковый", "id": "m_resend"}],
        previous_response_id=None,
    )
    save_thread_meta(str(tmp_path), "cli", "t1", {"messages_count": 3})

    assert discard_message_by_id(str(tmp_path), "cli", "t1", "m_orphan") is True

    msgs, _, _ = load_thread_messages(str(tmp_path), "cli", "t1")
    ids = [m.get("id") for m in msgs]
    assert "m_orphan" not in ids                 # осиротевшая удалена
    assert "m_resend" in ids and "a1" in ids     # повтор того же текста и вывод целы
    from gc_core.thread_store import load_thread_meta
    assert load_thread_meta(str(tmp_path), "cli", "t1")["messages_count"] == 2


def test_missing_id_is_noop(tmp_path):
    ts = _session(tmp_path)
    ts.persist_batch([{"role": "user", "content": "x", "id": "m1"}], previous_response_id=None)
    assert discard_message_by_id(str(tmp_path), "cli", "t1", "nope") is False
    msgs, _, _ = load_thread_messages(str(tmp_path), "cli", "t1")
    assert len(msgs) == 1


def test_no_file_is_noop(tmp_path):
    assert discard_message_by_id(str(tmp_path), "cli", "absent", "m1") is False
