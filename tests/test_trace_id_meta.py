from agent_executor import _append_trace_id_to_meta
from thread_store import load_thread_meta, save_thread_meta


class _SessionStub:
    def __init__(self, threads_dir):
        self.threads_dir = str(threads_dir)
        self.system_id = "cli"
        self.thread_id = "t1"


def test_appends_first_trace(tmp_path):
    session = _SessionStub(tmp_path)
    save_thread_meta(session.threads_dir, session.system_id, session.thread_id, {})
    _append_trace_id_to_meta(session, "wf-1")
    meta = load_thread_meta(session.threads_dir, session.system_id, session.thread_id)
    assert meta["runtime"]["trace_ids"] == ["wf-1"]


def test_appends_in_order_and_dedups(tmp_path):
    session = _SessionStub(tmp_path)
    save_thread_meta(session.threads_dir, session.system_id, session.thread_id, {})
    _append_trace_id_to_meta(session, "wf-1")
    _append_trace_id_to_meta(session, "wf-2")
    _append_trace_id_to_meta(session, "wf-1")
    meta = load_thread_meta(session.threads_dir, session.system_id, session.thread_id)
    assert meta["runtime"]["trace_ids"] == ["wf-1", "wf-2"]


def test_missing_identity_is_a_noop(tmp_path):
    class _Bare:
        threads_dir = None
        system_id = None
        thread_id = None

    _append_trace_id_to_meta(_Bare(), "wf-1")  # не должно бросать
