"""Флаг ``summarizing`` живого статуса: взведён в хранилище на время
сжатия, снят после — в том числе при ошибке."""

import pytest
from gc_core import run_summary_coordinator as rsc
from gc_core.thread_store import ThreadSession
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey

KEY = ThreadKey(system="cli", thread="t1")


async def _session(tmp_path):
    return await ThreadSession.afrom_config(
        settings=None,
        system_id=KEY.system,
        thread_id=KEY.thread,
        store=FileThreadStore(tmp_path),
    )


async def _flag_on_disk(tmp_path) -> bool:
    record = await FileThreadStore(tmp_path).get_thread(KEY)
    return record is not None and "summarizing" in record.runtime.flags


async def test_flag_set_during_and_cleared_after(tmp_path):
    ts = await _session(tmp_path)
    async with rsc._asummarizing_flag(ts):
        assert await _flag_on_disk(tmp_path)
        assert "summarizing" in ts.runtime.flags
    assert not await _flag_on_disk(tmp_path)
    assert "summarizing" not in ts.runtime.flags


async def test_flag_cleared_on_exception(tmp_path):
    ts = await _session(tmp_path)
    with pytest.raises(ValueError):
        async with rsc._asummarizing_flag(ts):
            raise ValueError("boom")
    assert not await _flag_on_disk(tmp_path)
    assert "summarizing" not in ts.runtime.flags


async def test_flag_noop_when_no_thread_session():
    async with rsc._asummarizing_flag(None):
        pass
