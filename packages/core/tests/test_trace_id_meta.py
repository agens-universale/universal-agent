from gc_core.agent_executor import _append_trace_id_to_meta
from gc_core.thread_store import load_thread_meta, save_thread_meta


class _SessionStub:
    """Похожа на настоящую ThreadSession — в том числе полем ``store``.

    Оно появилось здесь не для галочки: запись trace_id идёт через хранилище
    сессии, а не через модульные функции, иначе у встраивающего с базой эти
    записи молча уходили бы на диск мимо всего остального.
    """

    def __init__(self, threads_dir):
        from gc_core.thread_store_file import FileThreadStore

        self.threads_dir = str(threads_dir)
        self.system_id = "cli"
        self.thread_id = "t1"
        self.store = FileThreadStore(self.threads_dir)


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
