import pytest
from gc_ua_temporalio.config import AgentProfile
from gc_ua_temporalio.identity import session_workflow_id
from gc_ua_temporalio.storage.profiles import ProfilePins
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore


def test_revisions_are_immutable_across_worker_processes(tmp_path):
    path = tmp_path / "db"
    profile = AgentProfile(
        workspace=str(tmp_path),
        model="one",
        completions_url="http://localhost:1",
    )
    ProfilePins(SQLiteThreadStore(path)).pin("agent", "demo", profile)
    restarted = ProfilePins(SQLiteThreadStore(path))
    restarted.pin("agent", "demo", profile)
    with pytest.raises(ValueError, match="changed"):
        restarted.pin(
            "agent", "demo", profile.model_copy(update={"model": "two"})
        )
    restarted.pin(
        "agent",
        "demo",
        profile.model_copy(update={"model": "two", "revision": "v2"}),
    )


def test_workflow_identity_cannot_confuse_system_and_thread():
    assert session_workflow_id("a-b", "c") != session_workflow_id("a", "b-c")


def test_full_mailbox_still_acknowledges_duplicates_and_does_not_lose_overflow(
    tmp_path,
):
    from gc_ua_temporalio.session_contracts import InboxRequest, UserMessage
    from gc_ua_temporalio.storage.inbox import InboxLedger

    ledger = InboxLedger(SQLiteThreadStore(tmp_path / "inbox"))
    for i in range(64):
        ledger.accept(InboxRequest("owner", UserMessage(str(i), "hello")))
    assert (
        ledger.accept(InboxRequest("owner", UserMessage("0", "hello")))[
            "disposition"
        ]
        == "ACCEPTED"
    )
    with pytest.raises(ValueError, match="Mailbox is full"):
        ledger.accept(InboxRequest("owner", UserMessage("overflow", "hello")))
    with ledger.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM inbox").fetchone()[0] == 64
