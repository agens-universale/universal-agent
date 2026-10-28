"""Ход, упавший после захвата треда, отпускает тред и снимает отметку хода;
отметка, записанная после потери или отпускания аренды, не трогает runtime
нового владельца.

Ревью, раунд 4: сбой сразу после захвата (проверка старых ключей памяти)
оставлял аренду на срок и ``turn == "running"`` — повтор получал «тред занят»
вместо настоящей ошибки, а восстановление вебхука поднимало упавшую задачу
без конца. Поздняя отметка хода без аренды затирала runtime того, кто уже
взял тред (запись сбоку хранилище по сроку не фенсит).
"""

import importlib
import json
import pathlib
import types

import pytest
from gc_core.execution.environment import utc_now
from gc_core.threads.errors import ThreadBusy
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_core.threads.rules import lease_expired
from gc_core.threads.session_writer import SessionWriter
from gc_testkit.settings import make_settings

KEY = ThreadKey(system="cli", thread="t")
OTHER = "other/2/bbbb"


def _cfg(tmp_path):
    return make_settings(
        {
            "paths.threads_data_path": str(tmp_path),
            "backend.api_mode": "chat_completions",
            "backend.completions_url": "http://test",
            "agent.model": "x",
            "agent.max_steps": 1,
            "agent.system_prompt_id": "v1",
            "logging.save_logs": False,
        }
    )


async def _executor(monkeypatch, tmp_path, loop_impl):
    from gc_core.run_context import acreate_run_context

    ae = importlib.import_module("gc_core.agent_executor")
    monkeypatch.setattr(ae, "gc", types.SimpleNamespace(collect=lambda: None))
    monkeypatch.setattr(ae, "arun_agent", loop_impl)
    ctx = await acreate_run_context(_cfg(tmp_path), auth_connector=None)
    return ae.AgentExecutor(ctx)


async def _finished(messages, state, ctx, **kw):
    del messages, state, ctx, kw
    return {"finished": True}


async def _record(tmp_path):
    return await FileThreadStore(tmp_path).get_thread(KEY)


def _held(record) -> bool:
    lease = record.lease
    return lease is not None and not lease_expired(lease, utc_now())


async def _seed_legacy_memory_key(tmp_path):
    """Тред прежней версии: в meta устаревший ключ памяти — его отвергает
    проверка состояния уже после захвата треда."""
    writer = SessionWriter(FileThreadStore(tmp_path), KEY, owner="seed/1/a")
    await writer.aclaim()
    await writer.acommit(
        append_messages=[{"id": "m0", "role": "user", "content": "старое"}]
    )
    await writer.arelease()
    metas = list(pathlib.Path(tmp_path).rglob("meta_*.json"))
    assert metas
    for meta in metas:
        data = json.loads(meta.read_text())
        data["memory_files"] = ["x.md"]
        meta.write_text(json.dumps(data))


async def test_failure_right_after_claim_releases_the_thread(
    monkeypatch, tmp_path
):
    await _seed_legacy_memory_key(tmp_path)
    first = await _executor(monkeypatch, tmp_path, _finished)
    with pytest.raises(Exception) as failed:
        await first.arun(["сделай"], thread_id=KEY.thread, system=KEY.system)
    assert not isinstance(failed.value, ThreadBusy)
    record = await _record(tmp_path)
    assert not _held(record)
    assert record.runtime.turn != "running"

    again = await _executor(monkeypatch, tmp_path, _finished)
    with pytest.raises(Exception) as repeated:
        await again.arun(["сделай"], thread_id=KEY.thread, system=KEY.system)
    # Повтор видит настоящую ошибку, а не «тред занят».
    assert type(repeated.value) is type(failed.value)


async def _take_thread_as_other(store) -> SessionWriter:
    """Другой процесс берёт тред и ставит свою отметку хода и счётчики."""
    other = SessionWriter(store, KEY, owner=OTHER)
    await other.aclaim()
    record = await store.get_thread(KEY)
    runtime = record.runtime.model_copy(
        update={"turn": "running", "image_seq": 7, "live_msg_count": 42}
    )
    await other.aside(set_runtime=runtime)
    return other


def _assert_other_runtime_intact(record) -> None:
    assert record.lease is not None and record.lease.owner == OTHER
    assert record.runtime.turn == "running"
    assert record.runtime.image_seq == 7
    assert record.runtime.live_msg_count == 42


async def test_failed_turn_marks_before_it_releases(monkeypatch, tmp_path):
    """Тред взяли сразу после отпускания аренды упавшим ходом: его отметка
    «ход окончен» к этому моменту уже записана, а не ложится поверх."""
    armed = {"on": False}
    real_release = FileThreadStore.release

    async def release(self, key, lease):
        await real_release(self, key, lease)
        if armed["on"]:
            armed["on"] = False
            await _take_thread_as_other(self)

    monkeypatch.setattr(FileThreadStore, "release", release)

    async def broken(messages, state, ctx, **kw):
        del messages, state, ctx, kw
        armed["on"] = True
        raise RuntimeError("модель недоступна")

    executor = await _executor(monkeypatch, tmp_path, broken)
    with pytest.raises(RuntimeError):
        await executor.arun(
            ["сделай"], thread_id=KEY.thread, system=KEY.system
        )
    _assert_other_runtime_intact(await _record(tmp_path))


async def test_turn_that_lost_its_lease_leaves_the_runtime_alone(
    monkeypatch, tmp_path
):
    """Аренда хода истекла, тред взял другой процесс: шаг хода — ThreadBusy,
    а выход хода не пишет runtime треда без аренды."""
    holder: dict = {}

    async def loop(messages, state, ctx, **kw):
        del messages, state, ctx, kw
        session = holder["executor"]._thread_session
        store = session.store
        record = await store.get_thread(KEY)
        await store.release(KEY, record.lease)  # срок аренды вышел
        await _take_thread_as_other(store)
        await session.writer.acommit(
            append_messages=[{"id": "a1", "role": "assistant", "content": "x"}]
        )
        return {"finished": True}

    executor = await _executor(monkeypatch, tmp_path, loop)
    holder["executor"] = executor
    with pytest.raises(ThreadBusy):
        await executor.arun(
            ["сделай"], thread_id=KEY.thread, system=KEY.system
        )
    _assert_other_runtime_intact(await _record(tmp_path))
