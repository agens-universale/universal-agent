"""tui summarizing → meta.runtime.summarizing; снимается (в т.ч. при ошибке)."""

import pytest
from gc_core import run_summary_coordinator as rsc


class _FakeTS:
    """Похожа на ThreadSession — с полем ``store``.

    Флаг ``summarizing`` пишется через хранилище сессии; файловое хранилище
    здесь как раз и делегирует в модульные функции, которые тест подменяет.
    """

    def __init__(self, tmp_path):
        from gc_core.thread_store_file import FileThreadStore

        self.threads_dir = str(tmp_path)
        self.system_id = "cli"
        self.thread_id = "t1"
        self.meta = {}
        self.store = FileThreadStore(self.threads_dir)
        self.flushed_states = []  # снимки наличия флага при каждом flush
        self._port = None

    @property
    def port(self):
        """Как у ``ThreadSession``: асинхронный порт над своим хранилищем."""
        if self._port is None:
            from gc_core.thread_store_async import athread_store

            self._port = athread_store(self.store)
        return self._port


def _patch_flush(monkeypatch, ts):
    original = ts.store.patch_runtime_flag

    def record(*args, **kwargs):
        result = original(*args, **kwargs)
        disk = ts.store.load_meta(ts.system_id, ts.thread_id)
        ts.flushed_states.append(
            bool((disk.get("runtime") or {}).get("summarizing"))
        )
        return result

    monkeypatch.setattr(ts.store, "patch_runtime_flag", record)


async def test_flag_set_during_and_cleared_after(monkeypatch, tmp_path):
    ts = _FakeTS(tmp_path)
    _patch_flush(monkeypatch, ts)
    seen_during = {}

    def fake_compaction():
        rt = ts.meta.get("runtime") or {}
        seen_during["flag"] = bool(rt.get("summarizing"))

    async with rsc._asummarizing_flag(ts):
        fake_compaction()
    assert seen_during["flag"] is True
    assert not (ts.meta.get("runtime") or {}).get("summarizing")
    assert ts.flushed_states == [True, False]


async def test_flag_cleared_on_exception(monkeypatch, tmp_path):
    ts = _FakeTS(tmp_path)
    _patch_flush(monkeypatch, ts)
    with pytest.raises(ValueError):
        async with rsc._asummarizing_flag(ts):
            raise ValueError("boom")
    assert not (ts.meta.get("runtime") or {}).get("summarizing")
    assert ts.flushed_states[-1] is False


async def test_flag_noop_when_no_thread_session():
    async with rsc._asummarizing_flag(None):
        pass
