"""Интеграционные проверки RunSummaryCoordinator.pre_backend_compaction_hook (шаг 3 спеки)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from gc_core import run_summary_coordinator as rsc
from gc_core.run_summary_coordinator import RunSummaryCoordinator
from gc_testkit import run_coro


def _minimal_state(*, messages: list):
    return SimpleNamespace(
        backend_api_mode="chat_completions",
        messages=list(messages),
        previous_response_id=None,
    )


@pytest.fixture
def ctx_stub():
    return SimpleNamespace(
        config_dict={"save_logs": False},
        summary_trace_path="",
        logger=None,
    )


def _patch_save_thread_meta(monkeypatch) -> None:
    """Флаг «идёт суммаризация» координатор ставит через ``ts.store``, а тот в
    этих тестах MagicMock — подменять в модуле нечего, вызов и так уходит
    в заглушку."""
    return


def _thread_session_mock() -> MagicMock:
    thread = MagicMock()
    thread.meta = {}
    thread.threads_dir = "/tmp/x"
    thread.system_id = "cli"
    thread.thread_id = "t1"
    # Ядро ходит в хранилище через асинхронный порт: флаги, персист и архив
    # ждутся нативно, поэтому двойники обязаны быть корутинными.
    thread.port = AsyncMock()
    thread.apersist_batch = AsyncMock()
    thread.aarchive_and_compact_summary_payload = AsyncMock(return_value={})
    return thread


def test_pre_backend_passes_full_history_including_suffix(
    monkeypatch, ctx_stub
):
    """Компакция читает весь несистемный хвост из state, включая preserved_batch (суффикс)."""
    sys_m = {"role": "system", "content": "s"}
    u1 = {"role": "user", "content": "a", "id": "1"}
    u2 = {"role": "user", "content": "b", "id": "2"}
    state = _minimal_state(messages=[sys_m, u1, u2])
    captured: dict = {}

    async def capture(**kwargs):
        from gc_core.summarize.compaction import without_system_messages

        st = kwargs["state"]
        captured["non_system"] = without_system_messages(list(st.messages))
        st.messages = [
            sys_m,
            {"role": "user", "content": "<summary/>", "id": "sum"},
        ]

    monkeypatch.setattr(rsc, "arun_summary_compaction", capture)
    monkeypatch.setattr(rsc, "exceeded_summary_triggers", lambda **_k: True)

    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=None,
        summary_trigger_chars=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
    )
    current = [u1, u2]
    run_coro(coord.apre_backend_compaction_hook(current, "user_ask"))

    assert captured.get("non_system") == [u1, u2]


def test_suffix_mismatch_returns_without_changing_state(monkeypatch, ctx_stub):
    sys_m = {"role": "system", "content": "s"}
    stale = {"role": "user", "content": "stale", "id": "u0"}
    msgs = [sys_m, stale]
    state = _minimal_state(messages=list(msgs))

    async def boom(**_kwargs):
        raise AssertionError("compaction must not run")

    monkeypatch.setattr(rsc, "arun_summary_compaction", boom)

    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=None,
        summary_trigger_chars=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
    )
    monkeypatch.setattr(rsc, "exceeded_summary_triggers", lambda **_k: True)

    current = [{"role": "user", "content": "other", "id": "diff"}]
    run_coro(coord.apre_backend_compaction_hook(current, "user_ask"))

    assert state.messages == [sys_m, stale]


def test_suffix_match_runs_and_restores_even_if_compaction_raises(
    monkeypatch, ctx_stub
):
    sys_m = {"role": "system", "content": "s"}
    u1 = {"role": "user", "content": "a", "id": "1"}
    u2 = {"role": "user", "content": "b", "id": "2"}
    state = _minimal_state(messages=[sys_m, u1, u2])

    async def fail(**_kwargs):
        raise RuntimeError("compaction simulated failure")

    monkeypatch.setattr(rsc, "arun_summary_compaction", fail)
    monkeypatch.setattr(rsc, "exceeded_summary_triggers", lambda **_k: True)

    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=None,
        summary_trigger_chars=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
    )

    current = [u1, u2]
    with pytest.raises(RuntimeError, match="simulated"):
        run_coro(coord.apre_backend_compaction_hook(current, "user_ask"))

    assert state.messages == [sys_m, u1, u2]


def test_current_messages_inplace_replaced_after_hook(monkeypatch, ctx_stub):
    sys_m = {"role": "system", "content": "s"}
    u1 = {"role": "user", "content": "a", "id": "1"}
    state = _minimal_state(messages=[sys_m, u1])

    async def mock_compact(**kwargs):
        st = kwargs["state"]
        st.messages = [
            sys_m,
            {"role": "user", "content": "<summary/>", "id": "sum"},
        ]

    monkeypatch.setattr(rsc, "arun_summary_compaction", mock_compact)
    monkeypatch.setattr(rsc, "exceeded_summary_triggers", lambda **_k: True)

    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=None,
        summary_trigger_chars=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
    )
    current = [u1]
    run_coro(coord.apre_backend_compaction_hook(current, "user_ask"))
    assert current == [u1]


def test_no_orphan_outputs_in_current_after_function_call_output(
    monkeypatch, ctx_stub
):
    sys_m = {"role": "system", "content": "s"}
    call = {
        "type": "function_call",
        "call_id": "ok",
        "name": "x",
        "arguments": "{}",
        "id": "c",
    }
    out_ok = {
        "type": "function_call_output",
        "call_id": "ok",
        "output": "{}",
        "id": "o1",
    }
    out_bad = {
        "type": "function_call_output",
        "call_id": "orphan",
        "output": "{}",
        "id": "o2",
    }

    batch = [out_bad, call, out_ok]
    state = _minimal_state(messages=[sys_m, *batch])

    async def mock_compact(**kwargs):
        st = kwargs["state"]
        st.messages = [
            sys_m,
            {"role": "user", "content": "<summary/>", "id": "sum"},
        ]

    monkeypatch.setattr(rsc, "arun_summary_compaction", mock_compact)
    monkeypatch.setattr(rsc, "exceeded_summary_triggers", lambda **_k: True)

    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=None,
        summary_trigger_chars=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
    )
    run_coro(coord.apre_backend_compaction_hook(batch, "function_call_output"))

    out_ids = [
        m.get("call_id")
        for m in batch
        if m.get("type") == "function_call_output"
    ]
    assert out_ids == ["ok"]
    assert all(
        batch[i].get("type") == "function_call"
        for i in range(0, len(batch), 2)
    )


def test_persist_batch_only_when_nonempty(monkeypatch, ctx_stub):
    sys_m = {"role": "system", "content": "s"}
    u1 = {"role": "user", "content": "a", "id": "1"}
    state = _minimal_state(messages=[sys_m, u1])

    async def mock_compact(**kwargs):
        st = kwargs["state"]
        st.messages = [
            sys_m,
            {"role": "user", "content": "<summary/>", "id": "sum"},
        ]

    monkeypatch.setattr(rsc, "arun_summary_compaction", mock_compact)
    monkeypatch.setattr(rsc, "exceeded_summary_triggers", lambda **_k: True)
    _patch_save_thread_meta(monkeypatch)

    thread = _thread_session_mock()
    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=thread,
        summary_trigger_chars=10,
        is_cli_mode=True,
        pre_backend_trigger_reason="pre_backend",
    )
    current = [u1]
    run_coro(coord.apre_backend_compaction_hook(current, "user_ask"))

    thread.apersist_batch.assert_awaited_once()
    assert thread.apersist_batch.await_args[0][0]


def test_persist_skipped_when_preserved_batch_empty(monkeypatch, ctx_stub):
    async def _noop_compact(**_k):
        return None

    monkeypatch.setattr(rsc, "arun_summary_compaction", _noop_compact)
    monkeypatch.setattr(rsc, "exceeded_summary_triggers", lambda **_k: True)
    _patch_save_thread_meta(monkeypatch)

    state = _minimal_state(messages=[{"role": "system", "content": "x"}])
    thread = _thread_session_mock()
    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=thread,
        summary_trigger_chars=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
    )
    orphan = {
        "type": "function_call_output",
        "call_id": "nope",
        "output": "",
        "id": "o",
    }

    run_coro(
        coord.apre_backend_compaction_hook([orphan], "function_call_output")
    )

    thread.apersist_batch.assert_not_awaited()


def test_function_call_output_clears_current_when_preservation_empty(
    monkeypatch, ctx_stub
):
    sys_m = {"role": "system", "content": "s"}
    orphan = {
        "type": "function_call_output",
        "call_id": "solo",
        "output": "",
        "id": "o",
    }
    state = _minimal_state(messages=[sys_m, orphan])

    async def mock_compact(**kwargs):
        st = kwargs["state"]
        st.messages = [
            sys_m,
            {"role": "user", "content": "<summary/>", "id": "sum"},
        ]

    monkeypatch.setattr(rsc, "arun_summary_compaction", mock_compact)
    monkeypatch.setattr(rsc, "exceeded_summary_triggers", lambda **_k: True)

    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=None,
        summary_trigger_chars=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
    )
    current = [orphan]
    run_coro(
        coord.apre_backend_compaction_hook(current, "function_call_output")
    )

    assert current == []
