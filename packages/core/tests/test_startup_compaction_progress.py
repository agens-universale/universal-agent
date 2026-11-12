import json
from types import SimpleNamespace

import pytest
from gc_core import processing_responses as pr
from gc_core import run_summary_coordinator as rsc
from gc_core.agent_executor import AgentExecutor
from gc_core.config_modules.root import AppConfig
from gc_core.run_context import acreate_run_context
from gc_core.settings import LoadedSettings, RuntimeEnv
from gc_core.summarize import compaction
from gc_core.utils.logging_module import release_run_logger
from gc_testkit.helpers.summary_sample import valid_summary_xml_for_tests
from gc_testkit.settings import fake_ctx


@pytest.mark.parametrize(
    ("old_size", "new_size", "sizes", "rounds"),
    [
        (0, 5700, [5], 1),
        (10000, 5700, [5], 1),
        (10000, 3, [3000], 2),
        (10000, 3, [4000, 2500, 5], 3),
        (10000, 2000, [5], 2),
    ],
)
async def test_startup_progress(
    monkeypatch, old_size, new_size, sizes, rounds
):
    current = [{"role": "user", "content": "x" * new_size, "id": "new"}]
    old = (
        [{"role": "user", "content": "o" * old_size, "id": "old"}]
        if old_size
        else []
    )
    state = SimpleNamespace(
        backend_api_mode="chat_completions",
        messages=[*old, *current],
        previous_response_id=None,
        shared=SimpleNamespace(working_memory={}),
        user_intent_log=[],
        current_user_intent=[],
        dialog_steps_offset=0,
    )
    ctx = fake_ctx(
        {"logging.save_logs": False},
        logger=None,
        summary_trace_path="",
        collect_garbage=lambda: None,
    )
    requests = []

    async def summary(**kwargs):
        size = sizes[min(len(requests), len(sizes) - 1)]
        requests.append(list(kwargs["full_history_messages"]))
        return {
            "raw_summary": valid_summary_xml_for_tests(seed="s" * size),
            "num_batches": 1,
            "last_dialog_steps": "",
            "trace": {},
        }

    monkeypatch.setattr(
        compaction, "asummarize_dialog_via_chat_completions", summary
    )
    coord = rsc.RunSummaryCoordinator(
        ctx=ctx,
        state=state,
        thread_session=None,
        summary_trigger_chars=2000,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
        deferred_startup_compaction=True,
    )
    await coord.apre_backend_compaction_hook(current, "user_ask")
    assert len(requests) == rounds
    assert state.messages[-1] == {
        "role": "user",
        "content": "x" * new_size,
        "id": "new",
    }
    assert sum(m.get("id") == "new" for m in state.messages) == 1


@pytest.mark.parametrize("legacy", [False, True])
async def test_full_executor_long_new_request(monkeypatch, tmp_path, legacy):
    monkeypatch.chdir(tmp_path)
    config = AppConfig.model_validate(
        {
            "model": "default",
            "backend": {"api_mode": "chat_completions", "vision": False},
            "logging": {"save_logs": False, "console_trace": False},
            "agent": {
                "strict_turn_finish_validation": False,
                "stop_tools": ["project_memory"],
                "use_local_subagents": False,
            },
            "summary": {
                "chat_completions_summary_trigger_chars": 2000,
                "chat_completions_summary_turn_end_trigger_chars": 0,
            },
        }
    )
    ctx = await acreate_run_context(
        LoadedSettings(
            settings=config, runtime=RuntimeEnv(launch_root=str(tmp_path))
        )
    )
    if legacy:
        ctx.execution_patch = lambda name: (
            name != "startup-compaction-progress-v1"
        )
    summaries, main, sent = [], [], []

    async def summary(**kwargs):
        summaries.append(len(kwargs["full_history_messages"]))
        return {
            "raw_summary": valid_summary_xml_for_tests(seed="short"),
            "num_batches": 1,
            "last_dialog_steps": "",
            "trace": {},
        }

    async def backend(messages, state, ctx, *, resolved, **kwargs):
        main.append(len(summaries))
        sent.extend(state.messages)
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
        compaction, "asummarize_dialog_via_chat_completions", summary
    )
    monkeypatch.setattr(pr, "_arequest_backend_canonical", backend)
    try:
        await AgentExecutor(ctx).arun(
            ["x" * 5700], system="test", message_ids=["new"]
        )
    finally:
        release_run_logger(ctx.logger)
    assert main == ([20] if legacy else [1])
    assert any(
        m.get("id") == "new" and "x" * 5700 in m.get("content", "")
        for m in sent
    )


async def test_progressive_startup_keeps_round_cap(monkeypatch):
    current = [{"role": "user", "content": "new", "id": "new"}]
    state = SimpleNamespace(
        backend_api_mode="chat_completions",
        messages=[
            {"role": "user", "content": "old" * 30000, "id": "old"},
            *current,
        ],
        previous_response_id=None,
        shared=SimpleNamespace(working_memory={}),
        user_intent_log=[],
        current_user_intent=[],
        dialog_steps_offset=0,
    )
    ctx = fake_ctx(
        {"logging.save_logs": False},
        logger=None,
        summary_trace_path="",
        collect_garbage=lambda: None,
    )
    requests = []

    async def summary(**kwargs):
        size = 50000 - 1000 * len(requests)
        requests.append(list(kwargs["full_history_messages"]))
        return {
            "raw_summary": valid_summary_xml_for_tests(seed="s" * size),
            "num_batches": 1,
            "last_dialog_steps": "",
            "trace": {},
        }

    monkeypatch.setattr(
        compaction, "asummarize_dialog_via_chat_completions", summary
    )
    coord = rsc.RunSummaryCoordinator(
        ctx=ctx,
        state=state,
        thread_session=None,
        summary_trigger_chars=2000,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
        deferred_startup_compaction=True,
    )
    await coord.apre_backend_compaction_hook(current, "user_ask")
    assert len(requests) == 20
    assert state.messages[-1] == {
        "role": "user",
        "content": "new",
        "id": "new",
    }
