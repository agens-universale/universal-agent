"""Ход берёт тред явно в начале прогона (спека сервера, 13.1).

Чужая живая аренда — ``ThreadBusy`` до любой работы: цикл агента не
вызывается, модель запроса не получает. Аренда отпускается и в конце хода, и
при сбое подготовки — иначе тред держался бы до истечения срока.
"""

import importlib
import types

import pytest
from gc_core.threads.errors import ThreadBusy
from gc_core.threads.facade import Threads
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_core.threads.session_port import SessionPort
from gc_testkit.settings import make_settings

KEY = ThreadKey(system="cli", thread="t")


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


async def _executor(monkeypatch, tmp_path, calls: list):
    from gc_core.run_context import acreate_run_context

    ae = importlib.import_module("gc_core.agent_executor")
    monkeypatch.setattr(ae, "gc", types.SimpleNamespace(collect=lambda: None))

    async def _stub(messages, state, ctx, **kw):
        calls.append(messages)
        return {"finished": True}

    monkeypatch.setattr(ae, "arun_agent", _stub)
    ctx = await acreate_run_context(_cfg(tmp_path), auth_connector=None)
    return ae.AgentExecutor(ctx)


async def _other_takes(tmp_path) -> SessionPort:
    other = SessionPort(FileThreadStore(tmp_path), owner="other/1/abcdef12")
    await other.aclaim(KEY.system, KEY.thread)
    return other


async def test_busy_thread_refuses_before_the_loop(monkeypatch, tmp_path):
    calls: list = []
    ex = await _executor(monkeypatch, tmp_path, calls)
    await _other_takes(tmp_path)
    with pytest.raises(ThreadBusy) as busy:
        await ex.arun(["сделай"], thread_id=KEY.thread, system=KEY.system)
    assert busy.value.owner == "other/1/abcdef12"
    assert calls == []


async def test_run_after_release_goes_and_frees_the_thread(
    monkeypatch, tmp_path
):
    calls: list = []
    ex = await _executor(monkeypatch, tmp_path, calls)
    other = await _other_takes(tmp_path)
    await other.arelease_lease(KEY.system, KEY.thread)
    await ex.arun(["сделай"], thread_id=KEY.thread, system=KEY.system)
    assert len(calls) == 1
    # Ход кончился — тред свободен для другого владельца.
    await _other_takes(tmp_path)


async def test_setup_failure_releases_the_lease(monkeypatch, tmp_path):
    calls: list = []
    ex = await _executor(monkeypatch, tmp_path, calls)

    async def _broken(*_a, **_kw):
        raise RuntimeError("сборка состояния упала")

    monkeypatch.setattr(ex, "_abuild_run_state", _broken)
    with pytest.raises(RuntimeError):
        await ex.arun(["сделай"], thread_id=KEY.thread, system=KEY.system)
    await _other_takes(tmp_path)


async def test_facade_write_does_not_block_the_next_run(monkeypatch, tmp_path):
    """Посев истории фасадом (chat completions, техсообщение TUI) перед ходом
    не держит тред: ход берёт его сразу."""
    calls: list = []
    ex = await _executor(monkeypatch, tmp_path, calls)
    await Threads(FileThreadStore(tmp_path)).append_messages(
        KEY, [{"id": "seed", "role": "user", "content": "до хода"}]
    )
    await ex.arun(["сделай"], thread_id=KEY.thread, system=KEY.system)
    assert len(calls) == 1
