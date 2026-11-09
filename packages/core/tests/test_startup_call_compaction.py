"""Startup compaction uses the first prepared request, not the default slot."""

import json

import pytest
from gc_core import processing_responses as pr
from gc_core import run_summary_coordinator as rsc
from gc_core.agent_executor import AgentExecutor
from gc_core.auto_routing import judge_runner
from gc_core.config_modules.root import AppConfig
from gc_core.run_context import acreate_run_context
from gc_core.settings import LoadedSettings, RuntimeEnv
from gc_core.threads.facade import Threads
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_core.utils.logging_module import release_run_logger


@pytest.mark.parametrize("mode", ["auto", "max", "default"])
@pytest.mark.parametrize("threshold", [2000, 1000000])
async def test_startup_compaction_uses_first_call_and_keeps_current_task(
    tmp_path, monkeypatch, mode, threshold
):
    monkeypatch.chdir(tmp_path)
    config = AppConfig.model_validate(
        {
            "model": mode,
            "backend": {"api_mode": "chat_completions", "vision": False},
            "logging": {"save_logs": False, "console_trace": False},
            "agent": {
                "auto_routing_safe_level": "auto",
                "strict_turn_finish_validation": False,
                "stop_tools": ["project_memory"],
                "use_local_subagents": False,
            },
            "models": {
                alias: {
                    "model": alias,
                    "url": "http://local/v1/chat/completions",
                    "token": "",
                    "summary_trigger_chars": threshold
                    if alias == "big"
                    else 2000,
                }
                for alias in ("fast", "mid", "big")
            },
            "slots": {"light": "fast", "default": "mid", "max": "big"},
        }
    )
    store = FileThreadStore(tmp_path / "threads")
    await Threads(store).append_messages(
        ThreadKey(system="test", thread="resume"),
        [
            {"id": "old-user", "role": "user", "content": "old " * 2500},
            {"id": "old-answer", "role": "assistant", "content": "old result"},
        ],
    )
    ctx = await acreate_run_context(
        LoadedSettings(
            settings=config, runtime=RuntimeEnv(launch_root=str(tmp_path))
        ),
        thread_store=store,
    )
    compactions, requests, judges = [], [], []

    async def judge(**kwargs):
        judges.append(kwargs["messages"])
        return '{"score": 5, "reason": "complex task"}'

    async def compact(**kwargs):
        state = kwargs["state"]
        compactions.append(
            (
                state._call_level,
                kwargs["trigger_threshold_chars"],
                kwargs["trigger_reason"],
            )
        )
        assert kwargs.get("kept_messages")
        state.messages = [
            {
                "id": "summary",
                "role": "user",
                "content": "Previous work summary",
            }
        ]

    async def backend(messages, state, ctx, *, resolved, **kwargs):
        requests.append((resolved.alias, list(state.messages)))
        finish = {
            "type": "function_call",
            "id": "finish",
            "call_id": "finish",
            "name": "turn_finish",
            "status": "completed",
            "arguments": json.dumps(
                {
                    "solution_type": "full_solution",
                    "task": "test",
                    "task_report": "Done",
                    "handoff": "{}",
                }
            ),
        }
        return [finish], "reply", {}

    monkeypatch.setattr(
        judge_runner, "arequest_auxiliary_completion_text", judge
    )
    monkeypatch.setattr(rsc, "arun_summary_compaction", compact)
    monkeypatch.setattr(pr, "_arequest_backend_canonical", backend)
    try:
        await AgentExecutor(ctx).arun(
            ["NEW TASK: preserve this instruction"],
            system="test",
            thread_id="resume",
            message_ids=["new-task"],
        )
    finally:
        release_run_logger(ctx.logger)
    expected_level = "default" if mode == "default" else "max"
    expected_threshold = 2000 if mode == "default" else threshold
    expected_compactions = (
        [(expected_level, expected_threshold, "pre_run")]
        if expected_threshold == 2000
        else []
    )
    assert compactions == expected_compactions
    assert len(requests) == 1
    assert requests[0][0] == ("mid" if mode == "default" else "big")
    assert any(
        m.get("id") == "new-task" and "NEW TASK" in m.get("content", "")
        for m in requests[0][1]
    )
    assert len(judges) == (1 if mode == "auto" else 0)
    if judges:
        assert "NEW TASK" in json.dumps(judges[0])
