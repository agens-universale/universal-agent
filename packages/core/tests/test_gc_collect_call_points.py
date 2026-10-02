"""Тесты на вызов gc.collect() в точках C1/C2/C3 по спеке
docs_md/actual/2026-07-29-gc-collect-placement-spec.md.

C1 — 4 метода RunSummaryCoordinator (run_summary_coordinator.py) после успешной
      компакции; на no-op (early-return без компакции) collect не вызывается.
C2 — AgentExecutor.run finally после close_all (agent_executor.py).
C3 — processing_responses.aprocess_tool_calls перед return tools_outputs;
      условный по флагу gc.force_collect (default on CLI/TUI, off server).
"""

import importlib
import types

import pytest
from gc_testkit import run_coro

# ---------------------------------------------------------------------------
# C1: run_summary_coordinator
# ---------------------------------------------------------------------------


def _coord(
    monkeypatch,
    calls,
    gc_calls,
    backend="chat_completions",
    turn_end_trigger_chars=10_000,
    threshold_exceeded=True,
):
    """Собирает coordinator с замоканными compaction и gc.collect.

    ``calls`` — список аргументов run_summary_compaction (mock).
    ``gc_calls`` — список-счётчик вызовов gc.collect.
    ``threshold_exceeded`` управляет mock'ом exceeded_summary_triggers.
    """
    rsc = importlib.import_module("gc_core.run_summary_coordinator")
    state = types.SimpleNamespace(
        backend_api_mode=backend,
        messages=[{"role": "user", "content": "x" * 20_000}],
    )
    coord = rsc.RunSummaryCoordinator(
        ctx=types.SimpleNamespace(),
        state=state,
        thread_session=None,
        summary_trigger_chars=10_000,
        turn_end_trigger_chars=turn_end_trigger_chars,
        is_cli_mode=True,
        pre_backend_trigger_reason="pre_backend",
        logger=None,
    )

    async def _fake_compaction(**kw):
        calls.append(kw)

    monkeypatch.setattr(rsc, "arun_summary_compaction", _fake_compaction)
    monkeypatch.setattr(
        rsc, "exceeded_summary_triggers", lambda **kw: threshold_exceeded
    )
    monkeypatch.setattr(
        rsc, "gc", types.SimpleNamespace(collect=lambda: gc_calls.append(1))
    )
    return coord


class TestC1PreRunCompact:
    def test_collect_after_successful_pre_run(self, monkeypatch):
        calls, gc_calls = [], []
        coord = _coord(monkeypatch, calls, gc_calls, threshold_exceeded=True)
        run_coro(coord.apre_run_compact_if_needed())
        assert len(calls) >= 1
        assert len(gc_calls) == 1, (
            "gc.collect должен вызваться после успешной компакции"
        )

    def test_no_collect_when_threshold_not_exceeded(self, monkeypatch):
        calls, gc_calls = [], []
        coord = _coord(monkeypatch, calls, gc_calls, threshold_exceeded=False)
        run_coro(coord.apre_run_compact_if_needed())
        assert calls == []
        assert gc_calls == [], "no-op компакция не должна вызывать gc.collect"

    def test_no_collect_in_responses_mode(self, monkeypatch):
        calls, gc_calls = [], []
        coord = _coord(monkeypatch, calls, gc_calls, backend="responses")
        run_coro(coord.apre_run_compact_if_needed())
        assert calls == []
        assert gc_calls == []


class TestC1ForceCompactOnce:
    def test_collect_after_force_compact(self, monkeypatch):
        calls, gc_calls = [], []
        coord = _coord(monkeypatch, calls, gc_calls)
        run_coro(coord.aforce_compact_once())
        assert len(calls) == 1
        assert len(gc_calls) == 1

    def test_no_collect_in_responses_mode(self, monkeypatch):
        calls, gc_calls = [], []
        coord = _coord(monkeypatch, calls, gc_calls, backend="responses")
        run_coro(coord.aforce_compact_once())
        assert calls == []
        assert gc_calls == []


class TestC1PreBackendHook:
    def test_collect_after_pre_backend(self, monkeypatch):
        calls, gc_calls = [], []
        coord = _coord(monkeypatch, calls, gc_calls, threshold_exceeded=True)
        # _messages_tail_matches требует, чтобы state.messages заканчивался на
        # current_messages. Делаем state.messages = current_messages.
        current_messages = [{"role": "user", "content": "y"}]
        coord._state.messages = list(current_messages)
        run_coro(
            coord.apre_backend_compaction_hook(
                current_messages, mode="chat_completions"
            )
        )
        assert len(calls) == 1
        assert len(gc_calls) == 1

    def test_no_collect_when_threshold_not_exceeded(self, monkeypatch):
        calls, gc_calls = [], []
        coord = _coord(monkeypatch, calls, gc_calls, threshold_exceeded=False)
        run_coro(
            coord.apre_backend_compaction_hook([], mode="chat_completions")
        )
        assert calls == []
        assert gc_calls == []


class TestC1TurnEndCompact:
    def test_collect_after_turn_end(self, monkeypatch):
        calls, gc_calls = [], []
        coord = _coord(monkeypatch, calls, gc_calls, threshold_exceeded=True)
        run_coro(coord.aturn_end_compact_if_needed())
        assert len(calls) == 1
        assert len(gc_calls) == 1

    def test_no_collect_when_threshold_not_exceeded(self, monkeypatch):
        calls, gc_calls = [], []
        coord = _coord(monkeypatch, calls, gc_calls, threshold_exceeded=False)
        run_coro(coord.aturn_end_compact_if_needed())
        assert calls == []
        assert gc_calls == []

    def test_no_collect_when_turn_end_trigger_disabled(self, monkeypatch):
        calls, gc_calls = [], []
        coord = _coord(
            monkeypatch,
            calls,
            gc_calls,
            turn_end_trigger_chars=0,
            threshold_exceeded=True,
        )
        run_coro(coord.aturn_end_compact_if_needed())
        assert calls == []
        assert gc_calls == []


# ---------------------------------------------------------------------------
# C2: agent_executor finally
# ---------------------------------------------------------------------------


class TestC2AgentExecutorFinally:
    def _make_cfg(self, tmp_path):
        return {
            "threads_data_path": str(tmp_path),
            "backend_api_mode": "chat_completions",
            "completions_url": "http://test",
            "model": "x",
            "max_steps": 1,
            "system_prompt_id": "v1",
            "save_logs": False,
        }

    async def test_collect_called_in_finally(self, monkeypatch, tmp_path):
        """gc.collect вызывается в finally AgentExecutor.run после close_all.

        Честный end-to-end: реальный AgentExecutor.run доходит до finally
        (run_agent замокан на немедленный выход), gc.collect перехвачен.
        """
        from gc_core.run_context import acreate_run_context

        ae = importlib.import_module("gc_core.agent_executor")
        gc_calls = []
        monkeypatch.setattr(
            ae, "gc", types.SimpleNamespace(collect=lambda: gc_calls.append(1))
        )

        # run_agent — немедленный выход без HTTP/MCP.
        async def _arun_agent_stub(*a, **kw):
            return {"finished": True}

        monkeypatch.setattr(ae, "arun_agent", _arun_agent_stub)
        ctx = await acreate_run_context(
            self._make_cfg(tmp_path), auth_connector=None
        )
        ex = ae.AgentExecutor(ctx)
        await ex.arun(
            [{"role": "user", "content": "x"}],
            thread_id="t",
            start_new_thread=True,
            system="cli",
        )
        assert len(gc_calls) == 1, (
            "gc.collect должен вызваться в finally после close_all"
        )

    async def test_collect_called_even_on_exception_in_run(
        self, monkeypatch, tmp_path
    ):
        """finally срабатывает даже при исключении в теле run → gc.collect вызывается."""
        from gc_core.run_context import acreate_run_context

        ae = importlib.import_module("gc_core.agent_executor")
        gc_calls = []
        monkeypatch.setattr(
            ae, "gc", types.SimpleNamespace(collect=lambda: gc_calls.append(1))
        )

        async def _arun_agent_boom(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(ae, "arun_agent", _arun_agent_boom)
        ctx = await acreate_run_context(
            self._make_cfg(tmp_path), auth_connector=None
        )
        ex = ae.AgentExecutor(ctx)
        with pytest.raises(RuntimeError):
            await ex.arun(
                [{"role": "user", "content": "x"}],
                thread_id="t",
                start_new_thread=True,
                system="cli",
            )
        assert len(gc_calls) == 1, (
            "finally должен вызвать gc.collect даже при исключении"
        )


# ---------------------------------------------------------------------------
# C3: processing_responses conditional gc.collect
# ---------------------------------------------------------------------------


class TestC3ProcessingResponses:
    """C3: gc.collect перед return tools_outputs, под флагом gc.force_collect."""

    def _run_process_tool_calls(
        self, monkeypatch, *, config_dict, is_tui=False, is_cli_mode=False
    ):
        """Вызывает process_tool_calls с замоканным бэкендом и собирает
        флаг был ли вызван gc.collect."""
        pr = importlib.import_module("gc_core.processing_responses")
        gc_calls = []
        monkeypatch.setattr(
            pr, "gc", types.SimpleNamespace(collect=lambda: gc_calls.append(1))
        )

        # Замокаем _request_backend_canonical, чтобы не делать реальных запросов.
        async def _abackend_stub(messages, state, ctx):
            return [{"role": "assistant", "content": "ok"}], "rid", {}

        monkeypatch.setattr(pr, "_arequest_backend_canonical", _abackend_stub)
        ctx = types.SimpleNamespace(
            config_dict=config_dict,
            is_tui=is_tui,
            is_cli_mode=is_cli_mode,
        )
        state = types.SimpleNamespace(
            backend_api_mode="chat_completions",
            messages=[{"role": "user", "content": "hi"}],
            set_resp_headers_metadata=lambda h: None,
            previous_response_id=None,
            thread_id="t1",
        )
        # Пустой батч tool_calls → цикл не выполняется, session_compact нет →
        # доходит до C3 gc.collect перед return tools_outputs.
        messages = []

        async def _run_agent_func(*a, **kw):
            return None

        run_coro(
            pr.aprocess_tool_calls(
                messages, state, ctx, run_agent_func=_run_agent_func
            )
        )
        return gc_calls

    def test_collect_default_on_for_cli(self, monkeypatch):
        gc_calls = self._run_process_tool_calls(
            monkeypatch, config_dict={}, is_cli_mode=True
        )
        assert len(gc_calls) == 1, "default on для CLI"

    def test_collect_default_on_for_tui(self, monkeypatch):
        gc_calls = self._run_process_tool_calls(
            monkeypatch, config_dict={}, is_tui=True
        )
        assert len(gc_calls) == 1, "default on для TUI"

    def test_no_collect_default_off_for_server(self, monkeypatch):
        gc_calls = self._run_process_tool_calls(monkeypatch, config_dict={})
        assert gc_calls == [], "default off для server"

    def test_explicit_force_collect_true_overrides_server_off(
        self, monkeypatch
    ):
        gc_calls = self._run_process_tool_calls(
            monkeypatch, config_dict={"gc": {"force_collect": True}}
        )
        assert len(gc_calls) == 1

    def test_explicit_force_collect_false_overrides_cli_on(self, monkeypatch):
        gc_calls = self._run_process_tool_calls(
            monkeypatch,
            config_dict={"gc": {"force_collect": False}},
            is_cli_mode=True,
        )
        assert gc_calls == []
