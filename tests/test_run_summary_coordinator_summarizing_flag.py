"""tui summarizing → meta.runtime.summarizing; снимается (в т.ч. при ошибке)."""
import pytest

from gc_core import run_summary_coordinator as rsc
from gc_core import thread_store


class _FakeTS:
    def __init__(self):
        self.threads_dir = "/tmp/x"; self.system_id = "cli"; self.thread_id = "t1"
        self.meta = {}
        self.flushed_states = []  # снимки наличия флага при каждом flush


def _patch_flush(monkeypatch, ts):
    monkeypatch.setattr(thread_store, "load_thread_meta", lambda *a, **k: {})

    def fake_save(threads_dir, system_id, thread_id, meta):
        rt = meta.get("runtime") or {}
        ts.flushed_states.append(bool(rt.get("summarizing")))

    monkeypatch.setattr(thread_store, "save_thread_meta", fake_save)


def test_flag_set_during_and_cleared_after(monkeypatch):
    ts = _FakeTS()
    _patch_flush(monkeypatch, ts)
    seen_during = {}
    def fake_compaction():
        rt = ts.meta.get("runtime") or {}
        seen_during["flag"] = bool(rt.get("summarizing"))
    with rsc._summarizing_flag(ts):
        fake_compaction()
    assert seen_during["flag"] is True
    assert not (ts.meta.get("runtime") or {}).get("summarizing")
    assert ts.flushed_states == [True, False]


def test_flag_cleared_on_exception(monkeypatch):
    ts = _FakeTS()
    _patch_flush(monkeypatch, ts)
    with pytest.raises(ValueError):
        with rsc._summarizing_flag(ts):
            raise ValueError("boom")
    assert not (ts.meta.get("runtime") or {}).get("summarizing")
    assert ts.flushed_states[-1] is False


def test_flag_noop_when_no_thread_session():
    with rsc._summarizing_flag(None):
        pass
