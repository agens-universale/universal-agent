"""Trace path, формат summary_trace.jsonl и устойчивость pre_run (тест-стратегия спеки)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gc_core import run_summary_coordinator as rsc
from gc_core.summarize import summary_logging as summary_logging
from gc_core.run_summary_coordinator import RunSummaryCoordinator
from gc_testkit import run_coro


@pytest.fixture()
def coordinator_ctx(tmp_path):
    return SimpleNamespace(
        summary_trace_path="",
        config_dict={
            "save_logs": True,
            "logs_dump_dir": str(tmp_path / "logs"),
        },
        logger=None,
    )


async def test_summary_trace_prefers_ctx_path_over_logs_dump_dir(tmp_path, coordinator_ctx):
    custom = tmp_path / "custom_trace.jsonl"
    coordinator_ctx.summary_trace_path = str(custom)
    coordinator_ctx.config_dict["save_logs"] = True
    coordinator_ctx.config_dict["logs_dump_dir"] = str(tmp_path / "ignored_logs")

    state = SimpleNamespace(
        backend_api_mode="chat_completions",
        messages=[],
        previous_response_id=None,
    )

    coord = RunSummaryCoordinator(
        ctx=coordinator_ctx,
        state=state,
        thread_session=None,
        summary_trigger_chars=1,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
    )
    await coord.on_summary_applied(
        {
            "trigger_reason": "test_trigger",
            "summary_trace": {"k": "v"},
            "newly_summarized_message_id": "mid_1",
            "summary_payload": {},
        }
    )

    assert custom.exists()
    line = custom.read_text(encoding="utf-8").strip().split("\n")[0]
    parsed = json.loads(line)
    assert parsed["trigger_reason"] == "test_trigger"
    assert parsed["summary_trace"] == {"k": "v"}
    assert parsed["newly_summarized_message_id"] == "mid_1"


def test_pre_run_compact_swallows_exceptions(monkeypatch, coordinator_ctx):
    coordinator_ctx.summary_trace_path = ""

    state = SimpleNamespace(
        backend_api_mode="chat_completions",
        messages=[],
        previous_response_id=None,
    )

    coord = RunSummaryCoordinator(
        ctx=coordinator_ctx,
        state=state,
        thread_session=None,
        summary_trigger_chars=10,
        is_cli_mode=True,
        pre_backend_trigger_reason="pre_backend",
        logger=None,
    )

    monkeypatch.setattr(rsc, "history_exceeds_summary_trigger", lambda **_k: True)
    monkeypatch.setattr(
        rsc,
        "arun_summary_compaction",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("summary boom")),
    )

    run_coro(coord.apre_run_compact_if_needed(trigger_reason="pre_run"))


async def test_archive_called_on_summary_applied(monkeypatch, coordinator_ctx):
    state = SimpleNamespace(
        backend_api_mode="chat_completions",
        messages=[],
        previous_response_id=None,
    )
    thread = MagicMock()
    thread.port = AsyncMock()
    thread.aarchive_and_compact_summary_payload = AsyncMock(return_value={})
    coordinator_ctx.summary_trace_path = ""

    coord = RunSummaryCoordinator(
        ctx=coordinator_ctx,
        state=state,
        thread_session=thread,
        summary_trigger_chars=1,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
    )
    monkeypatch.setattr(summary_logging, "append_summary_trace_record", lambda *a, **k: None)

    await coord.on_summary_applied(
        {
            "trigger_reason": "x",
            "summary_trace": {},
            "newly_summarized_message_id": "",
            "summary_payload": {"p": 1},
        }
    )

    thread.aarchive_and_compact_summary_payload.assert_awaited_once_with({"p": 1})
