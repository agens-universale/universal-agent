import pytest
from gc_core.thread_store_durable import (
    ThreadStoreOperation,
    ThreadStoreOwnershipError,
)
from gc_core.thread_store_file import FileThreadStore
from gc_core.thread_store_maintenance import append_file_session_messages


def test_idle_ui_append_uses_session_policy(tmp_path):
    store = FileThreadStore(str(tmp_path))
    store.save_meta(
        "cli",
        "t",
        {"custom": {"kept": True}, "previous_response_id": "response"},
    )
    message = {"id": "technical", "role": "user", "content": "hello"}
    assert (
        append_file_session_messages(str(tmp_path), "cli", "t", [message]) == 1
    )
    snapshot = store.read_snapshot("cli", "t")
    assert snapshot.meta["messages_count"] == 1
    assert snapshot.meta["runtime"]["live_msg_count"] == 1
    assert snapshot.meta["runtime"]["live_history_chars"] > 0
    assert snapshot.meta["custom"] == {"kept": True}
    assert snapshot.meta["previous_response_id"] == "response"
    assert (
        append_file_session_messages(str(tmp_path), "cli", "t", [message]) == 0
    )
    assert store.load_meta("cli", "t")["runtime"]["live_msg_count"] == 1


@pytest.mark.parametrize("legacy_marker", [False, True])
def test_idle_ui_cannot_write_temporal_owned_thread(tmp_path, legacy_marker):
    store = FileThreadStore(str(tmp_path))
    if legacy_marker:
        store.save_meta(
            "cli",
            "t",
            {
                "execution_owner": {
                    "backend": "temporal",
                    "owner_id": "workflow",
                }
            },
        )
    else:
        store.commit_operation(
            "cli",
            "t",
            ThreadStoreOperation(
                "claim", "claim_owner", {"owner_id": "workflow"}
            ),
        )
    with pytest.raises(ThreadStoreOwnershipError):
        append_file_session_messages(
            str(tmp_path), "cli", "t", [{"id": "technical"}]
        )
    assert store.load_messages("cli", "t") == []
