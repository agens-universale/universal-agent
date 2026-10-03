"""Отмена закрытия MCP не должна терять итог уже завершённого цикла.

Учёт стоит после уборки, чтобы шаг после ``arun`` (перефраз) успел
дописать токены. Если уборка бросает ``CancelledError``, запись всё
равно происходит, а отмена доходит до вызывающего. Пока уборка проходит
без отмены и учёт отложен, токены перефраза попадают в ту же запись.
"""

import asyncio
import importlib
import types

import pytest
from gc_core.thread_store import load_thread_meta
from gc_core.usage.account import CATEGORY_MAIN, CATEGORY_STRUCTURED_OUTPUT
from gc_testkit.settings import make_settings


def _cfg(tmp_path):
    return make_settings(
        {
            "threads_data_path": str(tmp_path),
            "backend_api_mode": "chat_completions",
            "completions_url": "http://test",
            "model": "x",
            "max_steps": 1,
            "system_prompt_id": "v1",
            "trace_id": "trace-1",
            "save_logs": False,
        }
    )


def _release_cancel() -> None:
    """Снять отмену с задачи теста, иначе pytest сочтёт его отменённым."""
    task = asyncio.current_task()
    if task is None:
        return
    while task.cancelling():
        task.uncancel()


async def _executor(monkeypatch, tmp_path, loop, close):
    from gc_core.run_context import acreate_run_context

    ae = importlib.import_module("gc_core.agent_executor")
    monkeypatch.setattr(ae, "gc", types.SimpleNamespace(collect=lambda: None))
    monkeypatch.setattr(ae, "arun_agent", loop)
    monkeypatch.setattr(ae, "_aclose_mcp_and_release", close)
    ctx = await acreate_run_context(_cfg(tmp_path), auth_connector=None)
    return ae.AgentExecutor(ctx)


def _remember_outcome(state) -> None:
    state.shared.notes_md = "финальные заметки"
    state.usage.add(CATEGORY_MAIN, 100, 10)


def _meta(tmp_path):
    return load_thread_meta(str(tmp_path), "cli", "t")


async def _close_raises_cancel(_token):
    raise asyncio.CancelledError()


async def _close_cancels_task(_token):
    task = asyncio.current_task()
    assert task is not None
    task.cancel()
    await asyncio.sleep(0)


async def _close_ok(_token):
    return None


def _assert_finished_loop_on_disk(meta) -> None:
    shared = meta.get("shared") or {}
    assert shared.get("notes_md") == "финальные заметки", meta
    usage = (meta.get("token_usage") or {}).get("by_category") or {}
    main = usage.get(CATEGORY_MAIN) or {}
    assert main.get("input") == 100, meta
    assert main.get("output") == 10, meta
    runtime = meta.get("runtime") or {}
    assert runtime.get("trace_ids") == ["trace-1"], meta


async def test_session_cancel_during_mcp_close_persists_finished_loop(
    monkeypatch, tmp_path
):
    """``CancelledError`` из закрытия сессии не съедает запись цикла."""

    async def _loop(_messages, state, _ctx, **_kw):
        _remember_outcome(state)
        return "ok"

    ex = await _executor(monkeypatch, tmp_path, _loop, _close_raises_cancel)
    try:
        with pytest.raises(asyncio.CancelledError):
            await ex.arun(
                ["ход"],
                thread_id="t",
                start_new_thread=True,
                system="cli",
            )
        _assert_finished_loop_on_disk(_meta(tmp_path))
    finally:
        _release_cancel()


async def test_task_cancel_during_mcp_close_persists_finished_loop(
    monkeypatch, tmp_path
):
    """Снятие самой задачи тоже не пропускает запись: отмена снимается на время."""

    async def _loop(_messages, state, _ctx, **_kw):
        _remember_outcome(state)
        return "ok"

    ex = await _executor(monkeypatch, tmp_path, _loop, _close_cancels_task)
    try:
        with pytest.raises(asyncio.CancelledError):
            await ex.arun(
                ["ход"],
                thread_id="t",
                start_new_thread=True,
                system="cli",
            )
        _assert_finished_loop_on_disk(_meta(tmp_path))
    finally:
        _release_cancel()


async def test_unfinished_loop_is_not_persisted_when_mcp_close_cancels(
    monkeypatch, tmp_path
):
    """Упавший цикл не получает финальную запись из-за отмены уборки."""

    async def _loop(_messages, state, _ctx, **_kw):
        _remember_outcome(state)
        raise RuntimeError("ход упал")

    ex = await _executor(monkeypatch, tmp_path, _loop, _close_cancels_task)
    try:
        with pytest.raises(RuntimeError, match="ход упал"):
            await ex.arun(
                ["ход"],
                thread_id="t",
                start_new_thread=True,
                system="cli",
            )
        meta = _meta(tmp_path)
        shared = meta.get("shared") or {}
        assert shared.get("notes_md") != "финальные заметки"
        usage = (meta.get("token_usage") or {}).get("by_category") or {}
        assert CATEGORY_MAIN not in usage
    finally:
        _release_cancel()


async def test_deferred_accounting_still_includes_formatting_tokens(
    monkeypatch, tmp_path
):
    """Без отмены отложенный учёт видит токены, списанные уже после цикла."""

    async def _loop(_messages, state, _ctx, **_kw):
        _remember_outcome(state)
        return "ok"

    ex = await _executor(monkeypatch, tmp_path, _loop, _close_ok)
    await ex.arun(
        ["ход"],
        thread_id="t",
        start_new_thread=True,
        system="cli",
        account_run=False,
    )
    state = ex.get_state()
    assert state is not None
    state.usage.add(CATEGORY_STRUCTURED_OUTPUT, 7, 4)
    await ex.aclose_run_accounting(console=False)

    meta = _meta(tmp_path)
    _assert_finished_loop_on_disk(meta)
    usage = meta["token_usage"]["by_category"]
    structured = usage[CATEGORY_STRUCTURED_OUTPUT]
    assert structured["input"] == 7
    assert structured["output"] == 4
