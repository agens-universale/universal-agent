"""Интеграционные проверки RunSummaryCoordinator.pre_backend_compaction_hook (шаг 3 спеки)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from gc_core import run_summary_coordinator as rsc
from gc_core.run_summary_coordinator import (
    RunSummaryCoordinator,
)
from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx


def _minimal_state(*, messages: list):
    return SimpleNamespace(
        backend_api_mode="chat_completions",
        messages=list(messages),
        previous_response_id=None,
    )


@pytest.fixture
def ctx_stub():
    return fake_ctx(
        {"logging.save_logs": False}, summary_trace_path="", logger=None
    )


def _patch_save_thread_meta(monkeypatch) -> None:
    """Флаг «идёт суммаризация» координатор ставит через ``ts.store``, а тот в
    этих тестах MagicMock — подменять в модуле нечего, вызов и так уходит
    в заглушку."""
    return


def _thread_session_mock() -> MagicMock:
    thread = MagicMock()
    thread.threads_dir = "/tmp/x"
    thread.system_id = "cli"
    thread.thread_id = "t1"
    # Флаги, персист и архив сессия ждёт нативно — двойники корутинные.
    thread.aset_flag = AsyncMock()
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


def test_pre_backend_tells_compaction_which_messages_come_back(
    monkeypatch, ctx_stub
):
    """Сжатие узнаёт, что вернётся в историю как есть, — картинки этих сообщений
    не попадают в сводку как «вне контекста»."""
    sys_m = {"role": "system", "content": "s"}
    u1 = {"role": "user", "content": "a", "id": "1"}
    state = _minimal_state(messages=[sys_m, u1])
    captured: dict = {}

    async def capture(**kwargs):
        captured["kept"] = kwargs.get("kept_messages")
        kwargs["state"].messages = [sys_m]

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
    run_coro(coord.apre_backend_compaction_hook([u1], "user_ask"))

    assert captured["kept"] == [u1]


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

    assert batch == []
    assert state.messages[-1]["id"] == "sum"


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


def test_function_call_output_keeps_user_and_image_after_compaction(
    monkeypatch, ctx_stub
):
    """После сводки остаются реплика и картинка. Пара и напоминание сжаты."""
    sys_m = {"role": "system", "content": "s"}
    call = {
        "type": "function_call",
        "call_id": "c1",
        "name": "fs_read_file",
        "arguments": '{"path": "a.py"}',
        "id": "fc_c1",
    }
    out = {
        "type": "function_call_output",
        "call_id": "c1",
        "output": '{"ok": true}',
        "id": "fco_c1",
    }
    notice = {
        "role": "user",
        "content": '<technical type="technical">Loop detector: you called fs_read_file '
        "with the same arguments again.</technical>",
        "id": "m_notice",
    }
    human = {"role": "user", "content": "глянь файл", "id": "m_human"}
    image = {
        "role": "user",
        "content": '<technical type="technical">shot.png</technical>',
        "id": "m_image",
        "attachments": [{"id": "att1"}],
    }
    summary = {"role": "user", "content": "<summary/>", "id": "sum"}
    state = _minimal_state(messages=[sys_m, call, out, notice, human, image])

    async def mock_compact(**kwargs):
        kwargs["state"].messages = [sys_m, summary]

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
    current = [out, notice, human, image]
    run_coro(
        coord.apre_backend_compaction_hook(current, "function_call_output")
    )

    assert current == [human, image]
    assert state.messages == [sys_m, summary, human, image]


@pytest.mark.parametrize("rounds", [2, 20])
async def test_deferred_startup_preserves_current_batch_across_rounds(
    monkeypatch, ctx_stub, rounds
):
    old = {"role": "user", "content": "old history", "id": "old"}
    current = [{"role": "user", "content": "new task", "id": "new"}]
    state = _minimal_state(messages=[old, *current])
    calls = []

    async def compact(**kwargs):
        calls.append(kwargs["trigger_reason"])
        assert kwargs["kept_messages"] == current
        state.messages = [{"role": "user", "content": "summary", "id": "sum"}]

    monkeypatch.setattr(rsc, "arun_summary_compaction", compact)
    monkeypatch.setattr(
        rsc, "exceeded_summary_triggers", lambda **_kw: len(calls) < rounds
    )
    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=None,
        summary_trigger_chars=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
        deferred_startup_compaction=True,
        startup_progress_guard=False,
    )
    await coord.apre_backend_compaction_hook(current, "user_ask")
    assert calls == ["pre_run"] * rounds
    assert state.messages[-1] == current[-1]
    monkeypatch.setattr(rsc, "exceeded_summary_triggers", lambda **_kw: True)
    await coord.apre_backend_compaction_hook(current, "user_ask")
    assert calls == ["pre_run"] * rounds + ["pre_backend"]


async def test_deferred_startup_keeps_the_existing_round_limit(
    monkeypatch, ctx_stub
):
    current = [{"role": "user", "content": "new task", "id": "new"}]
    state = _minimal_state(messages=list(current))
    calls = []

    async def compact(**kwargs):
        calls.append(kwargs["trigger_reason"])
        state.messages = []

    monkeypatch.setattr(rsc, "arun_summary_compaction", compact)
    monkeypatch.setattr(rsc, "exceeded_summary_triggers", lambda **_kw: True)
    limit_logs = []
    monkeypatch.setattr(
        rsc, "log_pre_run_max_rounds", lambda *_a, **kw: limit_logs.append(kw)
    )
    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=None,
        summary_trigger_chars=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
        deferred_startup_compaction=True,
        startup_progress_guard=False,
    )
    await coord.apre_backend_compaction_hook(current, "user_ask")
    assert len(calls) == 20
    assert limit_logs[0]["max_rounds"] == 20
    assert current == [{"role": "user", "content": "new task", "id": "new"}]


@pytest.mark.parametrize("failure", ["trace", "cancel", "flag_cleanup"])
async def test_startup_batch_survives_failure_after_history_replacement(
    monkeypatch, ctx_stub, failure
):
    import asyncio
    from gc_core.attachments.model import Attachment

    image = Attachment(
        id="image",
        kind="image",
        mime="image/png",
        width=32,
        height=32,
        size=100,
        path="/tmp/task.png",
        origin="user",
        label="Изображение #1",
        source="/tmp/task.png",
        orig_width=32,
        orig_height=32,
    )
    task = {
        "role": "user",
        "content": "new task",
        "id": "new",
        "attachments": [image.to_dict()],
    }
    current = [task]
    state = _minimal_state(
        messages=[
            {"role": "user", "content": "old history", "id": "old"},
            task,
        ]
    )
    thread = _thread_session_mock()
    saved = []

    async def persist(messages, **_kwargs):
        saved.extend(messages)

    thread.apersist_batch.side_effect = persist
    error = asyncio.CancelledError if failure == "cancel" else RuntimeError

    async def compact(**kwargs):
        # Сводка применена к State и сохранена; следующий побочный эффект падает.
        summary = {"role": "user", "content": "summary", "id": "sum"}
        state.messages = [summary]
        saved[:] = [summary]
        if failure != "flag_cleanup":
            raise error("after summary replacement")

    async def flag(_thread, _name, active):
        if not active and failure == "flag_cleanup":
            raise error("flag cleanup")

    monkeypatch.setattr(rsc, "arun_summary_compaction", compact)
    monkeypatch.setattr(rsc, "_aset_runtime_flag", flag)
    monkeypatch.setattr(rsc, "exceeded_summary_triggers", lambda **_kw: True)
    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=thread,
        summary_trigger_chars=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
        deferred_startup_compaction=True,
    )
    with pytest.raises(error):
        await coord.apre_backend_compaction_hook(current, "user_ask")
    assert state.messages == [
        {"role": "user", "content": "summary", "id": "sum"},
        task,
    ]
    assert saved == state.messages
    assert current == [task]


async def test_batch_is_not_duplicated_when_compaction_fails_before_replacement(
    monkeypatch, ctx_stub
):
    current = [{"role": "user", "content": "new task", "id": "new"}]
    state = _minimal_state(messages=list(current))
    thread = _thread_session_mock()

    async def compact(**_kwargs):
        raise RuntimeError("before replacement")

    monkeypatch.setattr(rsc, "arun_summary_compaction", compact)
    monkeypatch.setattr(rsc, "exceeded_summary_triggers", lambda **_kw: True)
    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=thread,
        summary_trigger_chars=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
        deferred_startup_compaction=True,
    )
    with pytest.raises(RuntimeError):
        await coord.apre_backend_compaction_hook(current, "user_ask")
    assert state.messages == current
    thread.apersist_batch.assert_not_awaited()


@pytest.mark.parametrize("cancel", [False, True])
async def test_trace_failure_after_archive_keeps_task_in_real_thread(
    tmp_path, monkeypatch, ctx_stub, cancel
):
    import asyncio
    from gc_core.thread_store import ThreadSession
    from gc_core.threads.facade import Threads
    from gc_core.threads.file_store import FileThreadStore
    from gc_core.threads.models import ThreadKey

    task = {
        "role": "user",
        "content": "new task",
        "id": "new",
        "attachments": [
            {
                "id": "image",
                "kind": "image",
                "mime": "image/png",
                "width": 32,
                "height": 32,
                "size": 100,
                "path": "/tmp/task.png",
                "origin": "user",
                "label": "Изображение #1",
                "source": "/tmp/task.png",
                "orig_width": 32,
                "orig_height": 32,
            }
        ],
    }
    history = [{"role": "user", "content": "old history", "id": "old"}, task]
    store = FileThreadStore(tmp_path / "threads")
    key = ThreadKey(system="test", thread="thread")
    await Threads(store).append_messages(key, history)
    thread = await ThreadSession.afrom_config(
        settings=None, system_id="test", thread_id="thread", store=store
    )
    state = _minimal_state(messages=history)
    current = [task]
    trace_entered = asyncio.Event()

    async def trace(*_args, **_kwargs):
        trace_entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("trace failed after archive")

    async def compact(**kwargs):
        state.messages = [{"role": "user", "content": "summary", "id": "sum"}]
        await kwargs["on_summary_applied"](
            {
                "trigger_reason": "pre_run",
                "summary_payload": {"summary_message_content": "summary"},
            }
        )

    monkeypatch.setattr(rsc, "arun_summary_compaction", compact)
    monkeypatch.setattr(rsc, "aappend_summary_trace_record", trace)
    monkeypatch.setattr(rsc, "exceeded_summary_triggers", lambda **_kw: True)
    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=thread,
        summary_trigger_chars=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
        deferred_startup_compaction=True,
    )
    running = asyncio.create_task(
        coord.apre_backend_compaction_hook(current, "user_ask")
    )
    if cancel:
        await asyncio.wait_for(trace_entered.wait(), 5)
        running.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await running
    persisted = [e.data for e in await store.read_stream(key, "messages")]
    assert persisted[-1] == task
    assert state.messages[-1] == task
    assert len(persisted) == 2
    assert "summarizing" not in (await store.get_thread(key)).runtime.flags


async def test_old_execution_patch_retains_failure_command_order(
    monkeypatch, ctx_stub
):
    current = [{"role": "user", "content": "new task", "id": "new"}]
    state = _minimal_state(messages=list(current))
    thread = _thread_session_mock()
    patches = []
    ctx_stub.execution_patch = lambda name: patches.append(name) or False

    async def compact(**_kwargs):
        state.messages = [{"role": "user", "content": "summary", "id": "sum"}]
        raise RuntimeError("old history")

    monkeypatch.setattr(rsc, "arun_summary_compaction", compact)
    monkeypatch.setattr(rsc, "exceeded_summary_triggers", lambda **_kw: True)
    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=thread,
        summary_trigger_chars=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
    )
    with pytest.raises(RuntimeError):
        await coord.apre_backend_compaction_hook(current, "user_ask")
    assert patches == ["summary-batch-preservation-v1"]
    thread.apersist_batch.assert_not_awaited()


async def test_startup_no_progress_stop_is_logged(monkeypatch, ctx_stub):
    current = [{"role": "user", "content": "new task", "id": "new"}]
    state = _minimal_state(
        messages=[
            {"role": "user", "content": "old history", "id": "old"},
            *current,
        ]
    )
    calls = []

    async def compact(**kwargs):
        calls.append(kwargs["trigger_reason"])

    monkeypatch.setattr(rsc, "arun_summary_compaction", compact)
    monkeypatch.setattr(rsc, "exceeded_summary_triggers", lambda **_kw: True)
    stops = []
    monkeypatch.setattr(
        rsc, "log_pre_run_no_progress", lambda *_a, **kw: stops.append(kw)
    )
    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=None,
        summary_trigger_chars=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
        deferred_startup_compaction=True,
        startup_progress_guard=True,
    )
    await coord.apre_backend_compaction_hook(current, "user_ask")
    assert len(calls) == 1
    assert stops == [{"thread_id": None, "rounds": 1}]


def test_tool_output_in_history_is_compacted_with_it(monkeypatch, ctx_stub):
    """Ответ уже в истории. Сводка забирает его. После остаётся картинка."""
    sys_m = {"role": "system", "content": "s"}
    call = {
        "type": "function_call",
        "call_id": "c1",
        "name": "fs_read",
        "arguments": "{}",
        "id": "fc",
    }
    output = {
        "type": "function_call_output",
        "call_id": "c1",
        "output": "file body",
        "id": "fco",
    }
    image = {
        "role": "user",
        "content": '<technical type="technical">shot.png</technical>',
        "id": "img",
        "attachments": [{"id": "att1"}],
    }
    old = {"role": "user", "content": "old history", "id": "old"}
    summary = {"role": "user", "content": "<summary/>", "id": "sum"}
    state = _minimal_state(messages=[sys_m, old, call, output, image])
    captured: dict = {}

    async def mock_compact(**kwargs):
        captured["source"] = [m.get("id") for m in kwargs["state"].messages]
        captured["kept"] = [m.get("id") for m in kwargs["kept_messages"]]
        kwargs["state"].messages = [sys_m, summary]

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
    current = [output, image]
    run_coro(
        coord.apre_backend_compaction_hook(current, "function_call_output")
    )

    assert "fco" in captured["source"]
    assert captured["kept"] == ["img"]
    assert [m.get("id") for m in current] == ["img"]
    ids = [m.get("id") for m in state.messages]
    assert "fco" not in ids
    assert "fc" not in ids
    assert ids[-2:] == ["sum", "img"]


def test_tool_output_counts_toward_summary_threshold(monkeypatch, ctx_stub):
    """Длинный ответ в истории сам по себе перешагивает порог."""
    sys_m = {"role": "system", "content": "s"}
    call = {
        "type": "function_call",
        "call_id": "c1",
        "name": "fs_read",
        "arguments": "{}",
        "id": "fc",
    }
    output = {
        "type": "function_call_output",
        "call_id": "c1",
        "output": "x" * 400,
        "id": "fco",
    }
    state = _minimal_state(messages=[sys_m, call, output])
    called = {"yes": False}

    async def mock_compact(**kwargs):
        called["yes"] = True
        kwargs["state"].messages = [
            sys_m,
            {"role": "user", "content": "sum", "id": "sum"},
        ]

    monkeypatch.setattr(rsc, "arun_summary_compaction", mock_compact)
    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=None,
        summary_trigger_chars=50,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
    )

    run_coro(
        coord.apre_backend_compaction_hook([output], "function_call_output")
    )
    assert called["yes"] is True
