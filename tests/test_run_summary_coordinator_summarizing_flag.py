"""tui_summarizing флаг выставляется на время компакции и снимается (в т.ч. при ошибке)."""
import pytest

import run_summary_coordinator as rsc


class _FakeTS:
    def __init__(self):
        self.threads_dir = "/tmp/x"; self.system_id = "cli"; self.thread_id = "t1"
        self.meta = {}
        self.flushed_states = []  # снимки наличия флага при каждом flush


def _patch_flush(monkeypatch, ts):
    # Перехватываем save_thread_meta — записываем снимок наличия флага.
    def fake_save(threads_dir, system_id, thread_id, meta):
        ts.flushed_states.append(bool(meta.get("tui_summarizing")))
    monkeypatch.setattr(rsc, "save_thread_meta", fake_save, raising=False)


def test_flag_set_during_and_cleared_after(monkeypatch):
    ts = _FakeTS()
    _patch_flush(monkeypatch, ts)
    seen_during = {}
    def fake_compaction():
        seen_during["flag"] = bool(ts.meta.get("tui_summarizing"))
    with rsc._summarizing_flag(ts):
        fake_compaction()
    assert seen_during["flag"] is True          # внутри — флаг стоял
    assert ts.meta.get("tui_summarizing") is None  # после — снят
    assert ts.flushed_states == [True, False]   # flush на set и на clear


def test_flag_cleared_on_exception(monkeypatch):
    ts = _FakeTS()
    _patch_flush(monkeypatch, ts)
    with pytest.raises(ValueError):
        with rsc._summarizing_flag(ts):
            raise ValueError("boom")
    assert ts.meta.get("tui_summarizing") is None
    assert ts.flushed_states[-1] is False


def test_flag_noop_when_no_thread_session():
    # ts=None (CLI без треда) — не должно падать.
    with rsc._summarizing_flag(None):
        pass
