"""Turn-end compaction hook на RunSummaryCoordinator."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import run_summary_coordinator as rsc
from run_summary_coordinator import RunSummaryCoordinator


def _minimal_state(*, messages: list, backend_api_mode: str = "chat_completions"):
    return SimpleNamespace(
        backend_api_mode=backend_api_mode,
        messages=list(messages),
        previous_response_id=None,
        thread_id="thr1",
    )


@pytest.fixture
def ctx_stub():
    return SimpleNamespace(
        config_dict={"save_logs": False},
        summary_trace_path="",
        logger=None,
    )


def test_turn_end_skips_when_volume_below_threshold(monkeypatch, ctx_stub):
    state = _minimal_state(messages=[{"role": "user", "content": "x", "id": "1"}])
    monkeypatch.setattr(rsc, "history_exceeds_summary_trigger", lambda **_k: False)
    compaction = MagicMock()
    monkeypatch.setattr(rsc, "run_summary_compaction", compaction)

    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=None,
        summary_trigger_chars=100,
        turn_end_trigger_chars=50,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
    )
    coord.turn_end_compact_if_needed()
    compaction.assert_not_called()


def test_turn_end_runs_compaction_with_reason(monkeypatch, ctx_stub):
    sys_m = {"role": "system", "content": "s"}
    u1 = {"role": "user", "content": "a" * 100, "id": "1"}
    state = _minimal_state(messages=[sys_m, u1])
    captured: dict = {}

    def capture(**kwargs):
        captured.update(kwargs)
        state.messages = [sys_m, {"role": "user", "content": "<summary/>", "id": "sum"}]

    monkeypatch.setattr(rsc, "history_exceeds_summary_trigger", lambda **_k: True)
    monkeypatch.setattr(rsc, "run_summary_compaction", capture)

    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=None,
        summary_trigger_chars=200,
        turn_end_trigger_chars=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
    )
    coord.turn_end_compact_if_needed()

    assert captured.get("trigger_reason") == "turn_end"
    assert captured.get("state") is state


def test_turn_end_swallows_compaction_errors(monkeypatch, ctx_stub, capsys):
    state = _minimal_state(messages=[{"role": "user", "content": "x", "id": "1"}])

    def boom(**_kwargs):
        raise RuntimeError("summary down")

    monkeypatch.setattr(rsc, "history_exceeds_summary_trigger", lambda **_k: True)
    monkeypatch.setattr(rsc, "run_summary_compaction", boom)

    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=None,
        summary_trigger_chars=10,
        turn_end_trigger_chars=1,
        is_cli_mode=True,
        pre_backend_trigger_reason="pre_backend",
    )
    coord.turn_end_compact_if_needed()
    out = capsys.readouterr().out
    assert "Ошибка суммаризации" in out
    assert "turn_finish" in out


def test_turn_end_disabled_when_trigger_zero(monkeypatch, ctx_stub):
    state = _minimal_state(messages=[{"role": "user", "content": "x", "id": "1"}])
    compaction = MagicMock()
    monkeypatch.setattr(rsc, "history_exceeds_summary_trigger", lambda **_k: True)
    monkeypatch.setattr(rsc, "run_summary_compaction", compaction)

    coord = RunSummaryCoordinator(
        ctx=ctx_stub,
        state=state,
        thread_session=None,
        summary_trigger_chars=10,
        turn_end_trigger_chars=0,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
    )
    coord.turn_end_compact_if_needed()
    compaction.assert_not_called()
