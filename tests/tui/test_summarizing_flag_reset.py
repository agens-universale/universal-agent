"""reset_stale_summarizing_flag: протухший runtime.summarizing снимается на старте run'а."""
from thread_store import save_thread_meta, load_thread_meta
from run_summary_coordinator import reset_stale_summarizing_flag


class _StubSession:
    def __init__(self, threads_dir, meta):
        self.threads_dir = threads_dir
        self.system_id = "cli"
        self.thread_id = "t1"
        self.meta = meta


def test_reset_clears_stale_flag_on_disk_and_in_memory(tmp_path):
    save_thread_meta(
        str(tmp_path),
        "cli",
        "t1",
        {"runtime": {"summarizing": True}, "messages_count": 3},
    )
    ts = _StubSession(
        str(tmp_path),
        {"runtime": {"summarizing": True}, "messages_count": 3},
    )

    reset_stale_summarizing_flag(ts)

    assert not (ts.meta.get("runtime") or {}).get("summarizing")
    disk = load_thread_meta(str(tmp_path), "cli", "t1")
    assert not (disk.get("runtime") or {}).get("summarizing")
    assert disk.get("messages_count") == 3


def test_reset_noop_when_flag_absent(tmp_path):
    save_thread_meta(str(tmp_path), "cli", "t1", {"messages_count": 1})
    ts = _StubSession(str(tmp_path), {"messages_count": 1})
    reset_stale_summarizing_flag(ts)
    assert load_thread_meta(str(tmp_path), "cli", "t1").get("messages_count") == 1


def test_reset_none_session_is_noop():
    reset_stale_summarizing_flag(None)
