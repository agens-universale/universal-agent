"""Tests for the ATIF trajectory mapping in the Harbor external adapter.

Requires the `harbor` framework (dependency-group `test`).
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from stat import S_IMODE
from types import SimpleNamespace

import pytest

# tests/harbor/ (a test-fixture package) shadows the real harbor framework:
# reorder sys.path so real site-packages wins, then evict cached shadows.
_harbor_sp = next(
    (
        p
        for p in sys.path
        if (Path(p) / "harbor" / "agents" / "base.py").exists()
    ),
    None,
)
if _harbor_sp:
    sys.path.remove(_harbor_sp)
    sys.path.insert(0, _harbor_sp)
for _key in list(sys.modules.keys()):
    if _key == "harbor" or _key.startswith("harbor."):
        del sys.modules[_key]

# The adapter is not part of src/; import it as harbor does at runtime
# (`--agent-import-path adapters.gc_cli_external:GcCliExternal`), so its own
# `from adapters.gc_cli_config import ...` resolves the same way here.
from gc_testkit.paths import HARBOR

_HARBOR = HARBOR
sys.path.insert(0, str(_HARBOR))

from adapters.gc_cli_external import (
    GcCliExternal,
    _messages_to_steps,
    _stringify_content,
)
from harbor.models.agent.context import AgentContext


def test_roles_map_to_atif_sources_with_sequential_ids():
    steps = _messages_to_steps(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "do X"},
            {"role": "assistant", "content": "ok"},
        ]
    )
    assert [s.source for s in steps] == ["system", "user", "agent"]
    assert [s.step_id for s in steps] == [1, 2, 3]
    assert steps[2].message == "ok"


def test_tool_result_folds_into_last_agent_observation():
    steps = _messages_to_steps(
        [
            {"role": "assistant", "content": ""},
            {"role": "tool", "content": "exit=0"},
        ]
    )
    # The tool result does NOT create its own step (ATIF has no "tool" source).
    assert len(steps) == 1
    assert steps[0].source == "agent"
    assert steps[0].observation is not None
    assert steps[0].observation.results[0].content == "exit=0"


def test_orphan_tool_result_falls_back_to_user_step():
    steps = _messages_to_steps([{"role": "tool", "content": "orphan"}])
    assert len(steps) == 1
    assert steps[0].source == "user"
    assert steps[0].message == "orphan"


@pytest.mark.parametrize("content", ["answer", "", None])
def test_agent_reasoning_is_exported_separately(content):
    messages = [
        {
            "role": "assistant",
            "content": content,
            "reasoning_content": "inspect the failing route",
        }
    ]
    steps = _messages_to_steps(messages)
    assert steps[0].message == (content or "")
    assert steps[0].reasoning_content == "inspect the failing route"
    assert messages[0]["content"] == content


def test_non_agent_reasoning_is_not_exported():
    steps = _messages_to_steps(
        [
            {
                "role": "user",
                "content": "request",
                "reasoning_content": "ignored",
            },
            {"role": "assistant", "content": "answer"},
            {
                "role": "tool",
                "content": "result",
                "reasoning_content": "ignored",
            },
        ]
    )
    assert all(step.reasoning_content is None for step in steps)
    assert steps[1].observation.results[0].content == "result"


def test_trajectory_preserves_reasoning_after_history_compaction(tmp_path):
    from datetime import UTC, datetime

    from adapters.atif import TrajectoryRecorder
    from gc_core.execution.run_trace import ModelResponse

    adapter = GcCliExternal(logs_dir=tmp_path, model_name="openai/m")
    recorder = TrajectoryRecorder()
    first = {
        "role": "assistant",
        "content": "old",
        "reasoning_content": "old thought",
    }
    second = {
        "role": "assistant",
        "content": "",
        "reasoning_content": "new thought",
    }
    for index, response in enumerate((first, second)):
        recorder.observe(
            ModelResponse(
                f"response-{index}", "m", datetime.now(UTC), [response], None
            )
        )

    state = SimpleNamespace(messages=[second])
    assert adapter._write_trajectory(state, recorder.freeze()) == 2
    steps = json.loads((tmp_path / "trajectory.json").read_text())["steps"]
    assert [step["reasoning_content"] for step in steps] == [
        "old thought",
        "new thought",
    ]


def test_trajectory_records_no_tool_stop_reason(tmp_path):
    agent = GcCliExternal(logs_dir=tmp_path, model_name="openai/m")
    state = SimpleNamespace(
        messages=[{"role": "user", "content": "task"}],
        terminal_outcome="no_tool_response",
    )
    assert agent._write_trajectory(state) == 1
    payload = json.loads((tmp_path / "trajectory.json").read_text())
    assert payload["extra"]["terminal_outcome"] == "no_tool_response"
    assert [step["source"] for step in payload["steps"]] == ["user"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permission bits")
def test_trajectory_permissions_follow_creation_and_replacement_modes(
    tmp_path,
):
    agent = GcCliExternal(logs_dir=tmp_path, model_name="openai/m")
    state = SimpleNamespace(messages=[{"role": "user", "content": "task"}])
    reference = tmp_path / "reference.json"
    reference.write_text("{}")
    trajectory = tmp_path / "trajectory.json"

    assert agent._write_trajectory(state) == 1
    assert S_IMODE(trajectory.stat().st_mode) == S_IMODE(
        reference.stat().st_mode
    )

    trajectory.chmod(0o640)
    state.messages[0]["content"] = "updated task"
    assert agent._write_trajectory(state) == 1
    assert S_IMODE(trajectory.stat().st_mode) == 0o640
    assert (
        json.loads(trajectory.read_text())["steps"][0]["message"]
        == "updated task"
    )
    assert set(tmp_path.iterdir()) == {reference, trajectory}


def test_stringify_handles_none_str_and_parts():
    assert _stringify_content(None) == ""
    assert _stringify_content("x") == "x"
    assert (
        _stringify_content(
            [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        )
        == "a\nb"
    )


def test_gc_cli_config_overrides_merge_into_build_settings():
    from pathlib import Path

    agent = GcCliExternal(
        logs_dir=Path("/tmp"),
        model_name="openai/m",
        session_id="s",
        gc_cli_config={
            "backend.completions_url": "http://test/v1/chat/completions",
            "agent.no_tool_call_max_count": 5,
            "backend.chat_completion_kwargs": {"temperature": 0.7},
        },
    )
    cfg = agent._build_settings().settings
    # scalar override wins
    assert cfg.agent.no_tool_call_max_count == 5
    assert cfg.backend.chat_completion_kwargs == {"temperature": 0.7}


def test_partial_trajectory_written_when_run_aborts(tmp_path, monkeypatch):
    """A Harbor timeout / backend error aborts the worker before _run_loop returns.
    The adapter must still flush a partial trajectory from the executor's state and
    re-raise, so timed-out/errored runs leave a trace instead of a black hole.
    """
    partial_state = SimpleNamespace(
        messages=[
            {"role": "user", "content": "do X"},
            {"role": "assistant", "content": "partial work before crash"},
        ]
    )

    class FakeExecutor:
        def __init__(self, ctx):
            pass

        async def arun(self, messages):
            raise RuntimeError("HTTPError from backend")  # mimics the abort

        def get_state(self):
            return partial_state

    async def _actx(*_a, **_k):
        return SimpleNamespace()

    monkeypatch.setattr("gc_core.run_context.acreate_run_context", _actx)
    monkeypatch.setattr("gc_core.agent_executor.AgentExecutor", FakeExecutor)

    agent = GcCliExternal(
        logs_dir=tmp_path,
        model_name="openai/m",
        session_id="s",
        gc_cli_config={
            "backend.completions_url": "http://test/v1/chat/completions"
        },
    )
    context = SimpleNamespace(metadata=None)

    with pytest.raises(RuntimeError, match="HTTPError"):
        asyncio.run(agent.run("do X", environment=object(), context=context))

    traj = tmp_path / "trajectory.json"
    assert traj.exists()  # partial trace flushed despite the abort
    assert context.metadata["total_steps"] == 2


def _agent(**kwargs) -> GcCliExternal:
    kwargs.setdefault("gc_cli_config", {})
    kwargs["gc_cli_config"].setdefault(
        "backend.completions_url", "http://test/v1/chat/completions"
    )
    return GcCliExternal(
        logs_dir=Path("/tmp"), model_name="openai/m", **kwargs
    )


def test_polar_session_id_goes_into_every_llm_call(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = _agent(session_id="sympy__sympy-12419")._build_settings().settings
    headers = cfg.backend.chat_completion_headers
    # Lowercase key: it must REPLACE the core's internal state-UUID x-session-id.
    assert headers["x-session-id"] == "sympy__sympy-12419"
    assert headers["Authorization"] == "Bearer sympy__sympy-12419"


def test_profile_authorization_wins_over_session_bearer(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = (
        _agent(
            session_id="task-1",
            gc_cli_config={
                "backend.chat_completion_headers": {
                    "Authorization": "Bearer real-key"
                }
            },
        )
        ._build_settings()
        .settings
    )
    headers = cfg.backend.chat_completion_headers
    assert headers["Authorization"] == "Bearer real-key"
    assert headers["x-session-id"] == "task-1"  # session routing still applied


def test_session_id_falls_back_to_openai_api_key_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "task-from-env")
    cfg = _agent(api_base="http://polar/v1")._build_settings().settings
    assert (
        cfg.backend.chat_completion_headers["x-session-id"] == "task-from-env"
    )


def test_env_fallback_ignored_without_runner_api_base(monkeypatch):
    """A real key in the env must not become a session id in a local / otk run."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key")
    cfg = _agent()._build_settings().settings
    assert "x-session-id" not in cfg.backend.chat_completion_headers


def test_no_session_id_leaves_headers_untouched(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = _agent()._build_settings().settings
    assert "x-session-id" not in cfg.backend.chat_completion_headers


def test_runtime_url_and_model_beat_profile_overrides(monkeypatch):
    from pathlib import Path

    # Runtime sources (runner --model / env URL) are authoritative over profile.
    monkeypatch.setenv(
        "GC_CLI_BACKEND_URL", "http://runtime/v1/chat/completions"
    )
    agent = GcCliExternal(
        logs_dir=Path("/tmp"),
        model_name="openai/real-model",
        gc_cli_config={
            "backend.completions_url": "http://stale",
            "agent.model": "stale",
        },
    )
    cfg = agent._build_settings().settings
    assert (
        cfg.agent.model == "real-model"
    )  # openai/ stripped, profile value ignored
    assert cfg.backend.completions_url == "http://runtime/v1/chat/completions"


def _state_with_usage(**calls):
    """A State-like object carrying a real UsageAccount, as the run loop leaves it."""
    from gc_core.usage.account import CATEGORY_MAIN, UsageAccount

    account = UsageAccount()
    for _ in range(calls.pop("missing", 0)):
        account.add_missing(CATEGORY_MAIN)
    if calls:
        account.add(CATEGORY_MAIN, **calls)
    return SimpleNamespace(usage=account)


def test_usage_totals_reach_agent_context():
    context = AgentContext()
    state = _state_with_usage(
        input_tokens=14300, output_tokens=250, cached_tokens=1200
    )
    _agent()._populate_usage(context, state)
    # n_input_tokens includes cache, per AgentContext's contract.
    assert (
        context.n_input_tokens,
        context.n_output_tokens,
        context.n_cache_tokens,
    ) == (
        14300,
        250,
        1200,
    )


def test_trajectory_reports_trial_usage_without_inflating_step_metrics(
    tmp_path, monkeypatch
):
    from datetime import UTC, datetime

    from adapters.atif import TrajectoryRecorder
    from adapters.step_timings import StepTimings
    from gc_core.execution.run_trace import ModelResponse
    from gc_core.usage.account import (
        CATEGORY_FINISH_GATE,
        CATEGORY_MAIN,
        UsageAccount,
    )
    from gc_core.usage.context import record_usage, usage_scope

    usage = UsageAccount()
    usage.add(
        CATEGORY_MAIN,
        input_tokens=12,
        output_tokens=3,
        cached_tokens=8,
        model="m",
    )
    usage.add(
        CATEGORY_FINISH_GATE, input_tokens=20, output_tokens=2, model="judge"
    )
    with usage_scope(usage, CATEGORY_FINISH_GATE):
        record_usage({"prompt_tokens": 5}, model="judge")
        record_usage(None, model="judge")
    state = SimpleNamespace(state_id="local-run-id", usage=usage)
    trace = TrajectoryRecorder()
    trace.observe(
        ModelResponse(
            "reply",
            "m",
            datetime.now(UTC),
            [{"role": "assistant", "content": "Ready."}],
            {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 8},
            },
        )
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    adapter = GcCliExternal(
        logs_dir=tmp_path, model_name="openai/m", api_base="http://test/v1"
    )
    context = AgentContext()
    adapter._dump_outcome(
        state,
        trace,
        timings=StepTimings(tmp_path / "timings.jsonl"),
        context=context,
        partial=False,
    )

    payload = (tmp_path / "trajectory.json").read_text()
    assert "test-secret" not in payload
    trajectory = json.loads(payload)
    assert (
        trajectory["session_id"]
        == context.metadata["session_id"]
        == "local-run-id"
    )
    assert trajectory["final_metrics"] == {
        "total_steps": 1,
        "total_prompt_tokens": 12,
        "total_completion_tokens": 3,
        "total_cached_tokens": 8,
    }
    assert trajectory["extra"] == {
        "model_calls": 4,
        "atif_model_calls": 1,
        "atif_model_calls_match": False,
        "call_statistics": {"model_calls_without_usage": 1},
        "trial_usage": {
            "prompt_tokens": 37,
            "completion_tokens": 5,
            "cached_tokens": 8,
        },
    }
    assert context.metadata["llm_calls"] == 4
    assert (
        context.n_input_tokens,
        context.n_output_tokens,
        context.n_cache_tokens,
    ) == (37, 5, 8)
    assert context.metadata["llm_usage_by_model"] == {
        "m": {"input": 12, "output": 3, "cached": 8},
        "judge": {"input": 25, "output": 2, "cached": 0},
    }
    if "model_usage" in AgentContext.model_fields:
        assert context.model_usage["m"].n_input_tokens == 12
        assert context.model_usage["m"].n_cache_tokens == 8
        assert context.model_usage["judge"].n_input_tokens == 25
        assert context.model_usage["judge"].n_output_tokens == 2
        assert context.model_usage["judge"].cost_usd is None


def test_partial_trace_freezes_before_the_trial_usage_snapshot(tmp_path):
    from datetime import UTC, datetime

    from adapters.atif import TrajectoryRecorder
    from adapters.step_timings import StepTimings
    from gc_core.execution.run_trace import ModelResponse
    from gc_core.usage.account import CATEGORY_MAIN, UsageAccount

    trace = TrajectoryRecorder()
    timestamp = datetime.now(UTC)
    trace.observe(
        ModelResponse(
            "first",
            "model",
            timestamp,
            [],
            {"prompt_tokens": 10, "completion_tokens": 2},
        )
    )

    class RunningUsage(UsageAccount):
        def to_meta(self):
            snapshot = super().to_meta()
            self.add(
                CATEGORY_MAIN, input_tokens=20, output_tokens=3, model="model"
            )
            trace.observe(
                ModelResponse(
                    "late",
                    "model",
                    timestamp,
                    [],
                    {"prompt_tokens": 20, "completion_tokens": 3},
                )
            )
            return snapshot

    usage = RunningUsage()
    usage.add(CATEGORY_MAIN, input_tokens=10, output_tokens=2, model="model")
    context = AgentContext()
    adapter = GcCliExternal(logs_dir=tmp_path, model_name="openai/m")
    adapter._dump_outcome(
        SimpleNamespace(usage=usage),
        trace,
        timings=StepTimings(tmp_path / "timings.jsonl"),
        context=context,
        partial=True,
    )

    trajectory = json.loads((tmp_path / "trajectory.json").read_text())
    assert [step["extra"]["response_id"] for step in trajectory["steps"]] == [
        "first"
    ]
    assert trajectory["extra"]["partial"] is True
    assert (
        trajectory["extra"]["atif_model_calls"]
        == trajectory["extra"]["model_calls"]
        == 1
    )
    assert (
        trajectory["final_metrics"]["total_prompt_tokens"]
        == context.n_input_tokens
        == 10
    )
    assert context.metadata["llm_usage_by_model"] == {
        "model": {"input": 10, "output": 2, "cached": 0},
    }
    assert usage.totals()["input"] == 30


def test_usage_sums_across_categories():
    """Summariser, finish gate and subagent calls are part of what the run cost."""
    from gc_core.usage.account import (
        CATEGORY_MAIN,
        CATEGORY_SUBAGENT,
        CATEGORY_SUMMARY,
        UsageAccount,
    )

    account = UsageAccount()
    account.add(CATEGORY_MAIN, input_tokens=1000, output_tokens=100)
    account.add(CATEGORY_SUMMARY, input_tokens=500, output_tokens=50)
    account.add(CATEGORY_SUBAGENT, input_tokens=200, output_tokens=20)
    context = AgentContext()
    _agent()._populate_usage(context, SimpleNamespace(usage=account))
    assert (context.n_input_tokens, context.n_output_tokens) == (1700, 170)


def test_cost_is_left_to_the_downstream_tariff():
    """vllm returns no price and our rate card is in roubles — a USD figure would be made up."""
    from gc_core.usage.account import CATEGORY_MAIN, UsageAccount

    account = UsageAccount()
    account.add(CATEGORY_MAIN, input_tokens=10, output_tokens=1)
    context = AgentContext()
    _agent()._populate_usage(context, SimpleNamespace(usage=account))
    assert context.n_input_tokens == 10
    assert context.cost_usd is None


def test_no_calls_leaves_counters_unset():
    """Zero tokens would read as 'the run was free'; a timed-out run counted nothing."""
    context = AgentContext()
    assert _agent()._populate_usage(context, None) == (None, None)
    assert context.n_input_tokens is None

    totals, by_model = _agent()._populate_usage(context, _state_with_usage())
    assert totals["calls"] == 0
    assert by_model is None
    assert context.n_input_tokens is None


def test_calls_without_usage_are_reported_not_hidden():
    state = _state_with_usage(input_tokens=100, output_tokens=10, missing=3)
    totals, _ = _agent()._populate_usage(AgentContext(), state)
    assert (totals["calls"], totals["missing_usage_calls"]) == (4, 3)


def test_reporting_failure_never_breaks_the_run():
    """It runs inside a finally that may be unwinding a timeout."""

    class Exploding:
        @property
        def usage(self):
            raise RuntimeError("state is half-torn-down")

    assert _agent()._populate_usage(AgentContext(), Exploding()) == (
        None,
        None,
    )


def test_usage_by_model_reaches_the_metadata():
    """AUTO ходит двумя моделями: без разбивки снаружи нельзя посчитать цену
    прогона (спека PR140 Р8). Цены не считаются, cost_usd не трогается."""
    from gc_core.usage.account import CATEGORY_MAIN, UsageAccount

    account = UsageAccount()
    account.add(
        CATEGORY_MAIN,
        input_tokens=1000,
        output_tokens=100,
        model="vendor/fast",
    )
    account.add(
        CATEGORY_MAIN,
        input_tokens=300,
        output_tokens=30,
        cached_tokens=10,
        model="vendor/big",
    )
    _, by_model = _agent()._populate_usage(
        None, SimpleNamespace(usage=account)
    )
    assert by_model == {
        "vendor/fast": {"input": 1000, "output": 100, "cached": 0},
        "vendor/big": {"input": 300, "output": 30, "cached": 10},
    }


def test_incomplete_usage_account_never_breaks_finalization():
    state = SimpleNamespace(usage=SimpleNamespace(by_model={"m": None}))
    assert _agent()._populate_usage(AgentContext(), state) == (None, None)


def _captured_tool(call_id, name, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _captured_response(response_id, model, calls, usage, *, message=None):
    return {
        "id": response_id,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": message,
                    "tool_calls": calls,
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": usage,
    }


def _run_scripted_trajectory(tmp_path, monkeypatch, responses, **overrides):
    import httpx
    from gc_core import processing_responses as loop
    from gc_core.utils import connect_backend
    from gc_testkit.runtime_probe import probe_stdout
    from harbor.models.trajectories import Trajectory

    replies = iter(responses)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=next(replies))

    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **_kw: httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ),
    )

    class Environment:
        async def exec(self, command, **_kwargs):
            return SimpleNamespace(
                stdout=probe_stdout(command)
                or ("1\n" if "if test " in command else "/workspace\n"),
                stderr="",
                return_code=0,
            )

        async def is_dir(self, path, **_kwargs):
            return path == "/workspace"

    execute = loop.aapply_tool

    async def invoke(**kwargs):
        if kwargs["tool_name"] in {"exec_command", "fs_read_file"}:
            return json.dumps(
                {
                    "ok": True,
                    "result": kwargs["call_id"],
                    "tool_name": kwargs["tool_name"],
                }
            )
        return await execute(**kwargs)

    monkeypatch.setattr(loop, "aapply_tool", invoke)
    adapter = GcCliExternal(
        logs_dir=tmp_path,
        model_name="openai/requested",
        gc_cli_config={
            "backend.completions_url": "http://test/v1/chat/completions",
            "agent.strict_turn_finish_validation": False,
            "agent.use_local_subagents": False,
            "agent.max_steps": 5,
            **overrides,
        },
    )
    context = AgentContext()
    asyncio.run(adapter.run("Read both values.", Environment(), context))

    trajectory = Trajectory.model_validate_json(
        (tmp_path / "trajectory.json").read_text()
    )
    return trajectory, context, requests


def test_finish_gate_response_has_its_own_model_step(tmp_path, monkeypatch):
    from gc_testkit.scripted_model import FINISH
    from parity_harness import judge_accept

    finish = {**FINISH, "model": "provider/main"}
    judge = {
        **judge_accept(),
        "id": "judge-response",
        "model": "provider/judge",
    }
    trajectory, context, _ = _run_scripted_trajectory(
        tmp_path,
        monkeypatch,
        [finish, judge],
        **{"agent.strict_turn_finish_validation": True},
    )

    main, gate = [step for step in trajectory.steps if step.source == "agent"]
    assert main.tool_calls[0].function_name == "turn_finish"
    assert main.observation.results[0].source_call_id == "finish"
    assert gate.extra["usage_category"] == "finish_gate"
    assert gate.extra["response_id"] == "judge-response"
    assert gate.model_name == "provider/judge"
    assert gate.timestamp is not None and gate.timestamp >= main.timestamp
    assert gate.llm_call_count == 1
    assert gate.metrics.prompt_tokens == judge["usage"]["prompt_tokens"]
    assert (
        gate.metrics.completion_tokens == judge["usage"]["completion_tokens"]
    )
    assert (
        trajectory.final_metrics.total_prompt_tokens == context.n_input_tokens
    )
    assert (
        trajectory.extra["atif_model_calls"]
        == context.metadata["llm_calls"]
        == 2
    )
    assert trajectory.extra["atif_model_calls_match"] is True


def test_subagent_response_has_its_own_model_step(tmp_path, monkeypatch):
    from gc_testkit.scripted_model import FINISH

    delegation = _captured_tool(
        "delegate",
        "subagent_code_generator",
        {"task": "Summarize the assignment.", "reason": "Check delegation."},
    )
    first = _captured_response(
        "root-before-child",
        "provider/main",
        [delegation],
        {"prompt_tokens": 20, "completion_tokens": 7},
    )
    child = {**FINISH, "id": "child-response", "model": "provider/child"}
    last = {**FINISH, "id": "root-after-child", "model": "provider/main"}
    trajectory, context, _ = _run_scripted_trajectory(
        tmp_path,
        monkeypatch,
        [first, child, last],
        **{"agent.use_local_subagents": True},
    )

    start, delegated, finish = [
        step for step in trajectory.steps if step.source == "agent"
    ]
    assert start.observation.results[0].source_call_id == "delegate"
    assert delegated.extra["response_id"] == "child-response"
    assert delegated.extra["agent_scope"] == "subagent"
    assert delegated.extra["subagent_id"]
    assert delegated.extra["usage_category"] == "main"
    assert delegated.model_name == "provider/child"
    assert start.timestamp <= delegated.timestamp <= finish.timestamp
    assert delegated.llm_call_count == 1
    assert delegated.metrics.prompt_tokens == 10
    assert delegated.metrics.completion_tokens == 5
    assert finish.extra["response_id"] == "root-after-child"
    assert (
        trajectory.final_metrics.total_prompt_tokens
        == context.n_input_tokens
        == 40
    )
    assert (
        trajectory.final_metrics.total_completion_tokens
        == context.n_output_tokens
        == 17
    )
    assert (
        trajectory.extra["atif_model_calls"]
        == context.metadata["llm_calls"]
        == 3
    )
    assert trajectory.extra["atif_model_calls_match"] is True


def test_model_response_with_empty_choices_is_exported_once(
    tmp_path, monkeypatch
):
    response = {
        "id": "empty-choices",
        "model": "provider/main",
        "choices": [],
        "usage": {"prompt_tokens": 9, "completion_tokens": 0},
    }
    trajectory, context, _ = _run_scripted_trajectory(
        tmp_path,
        monkeypatch,
        [response],
        **{"agent.stop_on_no_tool_response": True},
    )

    (step,) = [step for step in trajectory.steps if step.source == "agent"]
    assert step.extra["response_id"] == "empty-choices"
    assert step.metrics.prompt_tokens == context.n_input_tokens == 9
    assert step.metrics.completion_tokens == 0
    assert (
        trajectory.extra["atif_model_calls"]
        == context.metadata["llm_calls"]
        == 1
    )


@pytest.mark.parametrize("with_tool", [False, True])
@pytest.mark.parametrize("reasoning_key", ["reasoning_content", "reasoning"])
def test_export_keeps_provider_reasoning_with_history_stripping(
    tmp_path, monkeypatch, with_tool, reasoning_key
):
    from gc_testkit.scripted_model import FINISH

    finish = FINISH["choices"][0]["message"]["tool_calls"]
    response = _captured_response(
        "reasoned-response",
        "provider/served",
        finish if with_tool else [],
        {"completion_tokens_details": {"reasoning_tokens": 3}},
        message="Done." if with_tool else None,
    )
    choice = response["choices"][0]
    choice["finish_reason"] = "tool_calls" if with_tool else "stop"
    choice["message"][reasoning_key] = " Inspect what remains.\n"

    trajectory, _, _ = _run_scripted_trajectory(
        tmp_path,
        monkeypatch,
        [response],
        **{
            "agent.stop_on_no_tool_response": True,
            "strip_tags_projection.strip_think": True,
        },
    )

    (step,) = [step for step in trajectory.steps if step.source == "agent"]
    assert step.reasoning_content == " Inspect what remains.\n"
    assert step.message == ("Done." if with_tool else "")
    assert step.metrics.extra == {"reasoning_tokens": 3}


@pytest.mark.parametrize(
    "missing_usage, compact", [(False, False), (True, False), (False, True)]
)
def test_run_exports_each_model_response_with_linked_results(
    tmp_path, monkeypatch, missing_usage, compact
):
    from datetime import UTC, datetime

    from gc_core import processing_responses as loop
    from gc_core.run_summary_coordinator import RunSummaryCoordinator
    from gc_core.summarize.compaction import apply_summary_state_patch
    from gc_core.summarize.llm import arequest_summary_text
    from gc_testkit.scripted_model import FINISH

    compactions = []
    summary_text = "Both values were read before compaction."
    prompts_with_summary = []
    create_call_data = loop.create_call_data_completions

    def capture_prompt(*args, **kwargs):
        data = create_call_data(*args, **kwargs)
        prompts_with_summary.append(
            any(
                summary_text in str(message["content"])
                for message in data["messages"]
                if message["role"] == "user"
            )
        )
        return data

    async def compact_outputs(self, messages, mode):
        if mode != "function_call_output" or compactions:
            return False
        result = await arequest_summary_text(
            ctx=self._ctx,
            state=self._state,
            messages=[{"role": "user", "content": "Summarize the two reads."}],
            request_label="summary_compress",
            consumer="summary",
        )
        apply_summary_state_patch(
            state=self._state,
            raw_summary=result,
            last_dialog_steps="",
            summarized_messages_count=len(self._state.messages),
            can_propose_plan=False,
        )
        messages.clear()
        compactions.append(True)
        return True

    if compact:
        monkeypatch.setattr(
            loop, "create_call_data_completions", capture_prompt
        )
        monkeypatch.setattr(
            RunSummaryCoordinator,
            "apre_backend_compaction_hook",
            compact_outputs,
        )
    calls = [
        _captured_tool(call_id, "exec_command", {"command": f"echo {marker}"})
        for call_id, marker in (("call-a", "alpha"), ("call-b", "beta"))
    ]
    first = _captured_response(
        "model-response-1",
        "provider/served",
        calls,
        {
            "prompt_tokens": 40,
            "completion_tokens": 9,
            "prompt_tokens_details": {"cached_tokens": 25},
            "completion_tokens_details": {"reasoning_tokens": 4},
        },
        message="Reading both values.",
    )
    second = {**FINISH, "id": "model-response-2", "model": "provider/served"}
    if missing_usage:
        del second["usage"]
    else:
        second["usage"] = {
            **second["usage"],
            "completion_tokens_details": {"reasoning_tokens": 0},
        }
    replies = [first]
    if compact:
        replies.append(
            _captured_response(
                "summary-response",
                "provider/summary",
                [],
                {
                    "prompt_tokens": 14,
                    "completion_tokens": 4,
                    "completion_tokens_details": {"reasoning_tokens": 2},
                },
                message=summary_text,
            )
        )
    replies.append(second)
    trajectory, context, _ = _run_scripted_trajectory(
        tmp_path, monkeypatch, replies
    )
    assert bool(compactions) == compact
    assert trajectory.schema_version == "ATIF-v1.7"
    assert [step.step_id for step in trajectory.steps] == list(
        range(1, len(trajectory.steps) + 1)
    )
    assert trajectory.steps[0].source == "system"
    assert trajectory.steps[1].source == "user"
    model_steps = [
        step
        for step in trajectory.steps
        if step.source == "agent" and step.extra["usage_category"] == "main"
    ]
    assert len(model_steps) == 2
    if compact:
        assert prompts_with_summary == [False, True]
        (summary_call,) = [
            step
            for step in trajectory.steps
            if step.source == "agent"
            and step.extra["usage_category"] == "summary"
        ]
        assert summary_call.extra["response_id"] == "summary-response"
        assert summary_call.model_name == "provider/summary"
        assert summary_call.llm_call_count == 1
        assert summary_call.metrics.prompt_tokens == 14
        assert summary_call.metrics.completion_tokens == 4
        assert (
            model_steps[0].timestamp
            <= summary_call.timestamp
            <= model_steps[1].timestamp
        )
        (summary_step,) = [
            step for step in trajectory.steps if summary_text in step.message
        ]
        assert summary_step.source == "user"
        assert (
            model_steps[0].step_id
            < summary_call.step_id
            < summary_step.step_id
            < model_steps[1].step_id
        )
    assert [step.llm_call_count for step in model_steps] == [1, 1]
    assert [step.model_name for step in model_steps] == ["provider/served"] * 2
    timestamps = [
        datetime.fromisoformat(step.timestamp) for step in model_steps
    ]
    assert all(ts.tzinfo == UTC for ts in timestamps)
    assert timestamps[0] <= timestamps[1]
    batch = model_steps[0]
    assert batch.message == "Reading both values."
    assert [call.tool_call_id for call in batch.tool_calls] == [
        "call-a",
        "call-b",
    ]
    assert batch.tool_calls[0].arguments == {"command": "echo alpha"}
    assert {
        item.source_call_id: json.loads(item.content)["result"]
        for item in batch.observation.results
    } == {"call-a": "call-a", "call-b": "call-b"}
    assert batch.metrics.prompt_tokens == 40
    assert batch.metrics.completion_tokens == 9
    assert batch.metrics.cached_tokens == 25
    assert batch.metrics.extra == {"reasoning_tokens": 4}
    assert (model_steps[1].metrics is None) == missing_usage
    if not missing_usage:
        assert model_steps[1].metrics.extra == {"reasoning_tokens": 0}
    assert model_steps[1].tool_calls[0].function_name == "turn_finish"
    assert model_steps[1].observation.results[0].source_call_id == "finish"
    expected_prompt = 40 if missing_usage else 50
    expected_completion = 9 if missing_usage else 14
    if compact:
        expected_prompt += 14
        expected_completion += 4
    assert trajectory.final_metrics.total_prompt_tokens == expected_prompt
    assert (
        trajectory.final_metrics.total_completion_tokens == expected_completion
    )
    assert trajectory.final_metrics.total_cached_tokens == 25
    assert trajectory.final_metrics.total_steps == len(trajectory.steps)
    assert trajectory.final_metrics.extra == (
        None if missing_usage else {"reasoning_tokens": 6 if compact else 4}
    )
    assert (
        trajectory.extra["atif_model_calls"]
        == context.metadata["llm_calls"]
        == 2 + compact
    )
    assert trajectory.extra["atif_model_calls_match"] is True
    assert (
        context.n_input_tokens == trajectory.final_metrics.total_prompt_tokens
    )


def test_auto_reroute_keeps_both_responses_and_uses_the_executed_call(
    tmp_path, monkeypatch
):
    from gc_testkit.scripted_model import FINISH

    read = _captured_tool(
        "read", "fs_read_file", {"path": "demo.txt", "add_line_numbers": False}
    )
    # The routed-away and executed calls can reuse an ID across responses.
    finish_call = FINISH["choices"][0]["message"]["tool_calls"][0]
    weak = _captured_tool(
        "finish", "fs_write_file", {"path": "demo.txt", "content": "weak"}
    )
    first = _captured_response(
        "read-response",
        "strong",
        [read],
        {"prompt_tokens": 50, "completion_tokens": 9},
    )
    discarded = _captured_response(
        "weak-response",
        "weak",
        [weak],
        {"prompt_tokens": 60, "completion_tokens": 5},
    )
    final = _captured_response(
        "finish-response",
        "strong",
        [finish_call],
        {"prompt_tokens": 70, "completion_tokens": 11},
    )
    trajectory, context, requests = _run_scripted_trajectory(
        tmp_path,
        monkeypatch,
        [first, discarded, final],
        model="auto",
        models={"weak": {}, "strong": {}},
        slots={"light": "weak", "default": "strong"},
        **{
            "agent.auto_routing_safe_level": "default",
            "agent.auto_routing_two_level": {
                "strong_steps": 1,
                "weak_steps": 2,
            },
        },
    )
    models = [request["model"] for request in requests]
    assert models == ["strong", "weak", "strong"]
    steps = [step for step in trajectory.steps if step.source == "agent"]
    assert [step.model_name for step in steps] == models
    assert [step.metrics.prompt_tokens for step in steps] == [50, 60, 70]
    assert steps[1].extra["discarded_by_routing"] is True
    assert steps[1].observation is None
    assert steps[2].observation.results[0].source_call_id == "finish"
    assert trajectory.final_metrics.total_prompt_tokens == 180
    assert context.metadata["llm_calls"] == 3


def test_cancel_flushes_tool_results_committed_by_the_stopping_worker(
    tmp_path, monkeypatch
):
    import threading

    from gc_core.execution.run_trace import (
        bind_trace_root,
        finish_model_trace,
        start_model_trace,
    )
    from gc_core.processing_responses import _maybe_persist
    from harbor.models.trajectories import Trajectory

    started, stopped = threading.Event(), threading.Event()
    state = SimpleNamespace(messages=[])

    class Executor:
        def __init__(self, ctx):
            pass

        async def arun(self, messages):
            with bind_trace_root(state):
                request = start_model_trace(state)
                finish_model_trace(
                    request,
                    {},
                    "m",
                    [
                        {
                            "type": "function_call",
                            "name": "exec_command",
                            "arguments": '{"command": "echo hello"}',
                            "call_id": "test-call",
                        }
                    ],
                    "test-response",
                )
                started.set()
                assert await asyncio.to_thread(stopped.wait, 3)
                await asyncio.sleep(0.02)
                await _maybe_persist(
                    None,
                    [
                        {
                            "type": "function_call_output",
                            "call_id": "test-call",
                            "output": "completed during cooperative stop",
                        }
                    ],
                    state,
                )

        def stop_run(self):
            stopped.set()

        def get_state(self):
            return state

    async def create_context(*_args, **_kwargs):
        return SimpleNamespace()

    async def skip_skills_export(*_args):
        pass

    monkeypatch.setattr(
        GcCliExternal, "_write_skills_debug", skip_skills_export
    )

    monkeypatch.setattr(
        "gc_core.run_context.acreate_run_context", create_context
    )
    monkeypatch.setattr("gc_core.agent_executor.AgentExecutor", Executor)
    adapter = GcCliExternal(
        logs_dir=tmp_path,
        model_name="openai/m",
        gc_cli_config={
            "backend.completions_url": "http://test/v1/chat/completions"
        },
    )

    async def cancel():
        task = asyncio.create_task(
            adapter.run("run", object(), AgentContext())
        )
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    trace = Trajectory.model_validate_json(
        (tmp_path / "trajectory.json").read_text()
    )
    assert trace.extra["partial"] is True
    (step,) = trace.steps
    (result,) = step.observation.results
    assert result.source_call_id == "test-call"
    assert result.content == "completed during cooperative stop"


@pytest.mark.parametrize("phase", ["queued", "attaching"])
def test_cancel_before_worker_initialization_cleans_up(
    tmp_path, monkeypatch, phase
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from adapters import gc_cli_external, step_timings

    release = threading.Event()
    context_starts = []
    sinks = []

    async def create_context(*_args, **_kwargs):
        context_starts.append(True)
        return SimpleNamespace()

    async def skip_skills_export(*_args):
        pass

    monkeypatch.setattr(
        "gc_core.run_context.acreate_run_context", create_context
    )
    monkeypatch.setattr(
        "gc_core.agent_executor.AgentExecutor",
        lambda _ctx: SimpleNamespace(get_state=lambda: None),
    )
    monkeypatch.setattr(
        GcCliExternal, "_write_skills_debug", skip_skills_export
    )
    monkeypatch.setattr(gc_cli_external, "_STOP_GRACE_S", 0.05)
    adapter = GcCliExternal(
        logs_dir=tmp_path,
        model_name="openai/m",
        gc_cli_config={
            "backend.completions_url": "http://test/v1/chat/completions"
        },
    )
    context = AgentContext()

    async def cancel():
        loop = asyncio.get_running_loop()
        ready = asyncio.Event()

        class RecordingTimings(gc_cli_external.StepTimings):
            def __init__(self, path):
                super().__init__(path)
                sinks.append(self)

            def attach(self):
                if phase == "attaching":
                    loop.call_soon_threadsafe(ready.set)
                    assert release.wait(5)
                super().attach()

        monkeypatch.setattr(gc_cli_external, "StepTimings", RecordingTimings)
        pool = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(pool)
        if phase == "queued":
            pool.submit(release.wait, 5)
        task = asyncio.create_task(adapter.run("run", object(), context))
        try:
            if phase == "queued":
                # The task has reached its first suspension, behind the occupied worker.
                await asyncio.sleep(0)
                assert sinks
            else:
                await asyncio.wait_for(ready.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            pool.shutdown(wait=True)

    asyncio.run(cancel())

    assert context_starts == []
    assert context.metadata["total_steps"] == 0
    (sink,) = sinks
    assert sink._fh is None
    assert sink not in step_timings._sinks.values()
    if phase == "queued":
        assert not (tmp_path / "timings.jsonl").exists()


def test_recorder_keeps_attempt_timestamps_when_enriching_retried_response():
    from datetime import UTC, datetime, timedelta

    from adapters.atif import TrajectoryRecorder
    from gc_core.execution.run_trace import BackendResponse, ModelResponse

    started_at = datetime(2026, 11, 23, tzinfo=UTC)
    first_attempt = started_at + timedelta(seconds=1)
    second_attempt = started_at + timedelta(seconds=4)
    recorder = TrajectoryRecorder()
    first = {"id": "reused-id", "usage": {"prompt_tokens": 6}}
    second = {"id": "reused-id", "usage": {"prompt_tokens": 12}}
    recorder.observe(
        BackendResponse(first, "m", first_attempt, "main", False, None)
    )
    recorder.observe(
        BackendResponse(second, "m", second_attempt, "main", False, None)
    )
    recorder.observe(
        ModelResponse(
            "reused-id",
            "m",
            started_at,
            [{"role": "assistant", "content": "Done"}],
            second["usage"],
            backend_payload=second,
        )
    )

    original, retried = recorder.freeze()
    assert original.timestamp == first_attempt.isoformat()
    assert original.metrics.prompt_tokens == 6
    assert retried.timestamp == second_attempt.isoformat()
    assert retried.message == "Done"
    assert retried.metrics.prompt_tokens == 12


def test_recorder_matches_out_of_order_results_and_ignores_late_events():
    from datetime import UTC, datetime

    from adapters.atif import TrajectoryRecorder
    from gc_core.execution.run_trace import CommittedMessages, ModelResponse

    recorded_at = datetime.now(UTC)
    calls = [
        {
            "type": "function_call",
            "call_id": call_id,
            "name": "exec_command",
            "arguments": "{}",
        }
        for call_id in ("a", "b", "c")
    ]
    recorder = TrajectoryRecorder()
    recorder.observe(ModelResponse("r", "m", recorded_at, calls, None))
    outputs = [
        {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(output),
        }
        for call_id, output in (
            ("b", {"stdout": "second", "exit_code": 7, "ok": False}),
            ("a", {"stdout": "first", "exit_code": 0, "ok": True}),
            ("c", {"stdout": "third", "exit_code": 7}),
        )
    ]
    recorder.observe(CommittedMessages(outputs, recorded_at))
    recorder.observe(CommittedMessages(outputs, recorded_at))

    (step,) = recorder.freeze()
    recorder.observe(
        CommittedMessages([{**outputs[0], "output": "late"}], recorded_at)
    )
    assert {
        result.source_call_id: json.loads(result.content)
        for result in step.observation.results
    } == {
        "a": {"stdout": "first", "exit_code": 0, "ok": True},
        "b": {"stdout": "second", "exit_code": 7, "ok": False},
        "c": {"stdout": "third", "exit_code": 7},
    }
    assert len(step.observation.results) == 3
    assert {
        result.source_call_id: result.extra
        for result in step.observation.results
    } == {
        "a": {"is_error": False},
        "b": {"is_error": True},
        "c": None,
    }


def test_malformed_call_identity_keeps_response_usage_and_valid_observation(
    tmp_path, monkeypatch
):
    from gc_testkit.scripted_model import FINISH

    calls = [
        _captured_tool(7, "exec_command", {"command": "echo ignored"}),
        _captured_tool("bad-name", "   ", {}),
        _captured_tool("valid", "exec_command", {"command": "echo hello"}),
    ]
    response = _captured_response(
        "mixed-batch",
        "provider/served",
        calls,
        {"prompt_tokens": 8, "completion_tokens": 3},
    )
    trajectory, context, _ = _run_scripted_trajectory(
        tmp_path,
        monkeypatch,
        [response, FINISH],
        **{"agent.stop_on_no_tool_response": True},
    )

    model_steps = [step for step in trajectory.steps if step.source == "agent"]
    assert len(model_steps) == 2
    first, final = model_steps
    assert first.extra["response_id"] == "mixed-batch"
    assert (first.metrics.prompt_tokens, first.metrics.completion_tokens) == (
        8,
        3,
    )
    assert [call.tool_call_id for call in first.tool_calls] == ["valid"]
    assert [item.source_call_id for item in first.observation.results] == [
        "valid"
    ]
    assert final.observation.results[0].source_call_id == "finish"
    assert (
        trajectory.final_metrics.total_prompt_tokens
        == context.n_input_tokens
        == 18
    )


@pytest.mark.parametrize("leading_invalid", [False, True])
def test_duplicate_call_id_uses_the_same_call_for_execution_and_trajectory(
    tmp_path, monkeypatch, leading_invalid
):
    from gc_testkit.scripted_model import FINISH

    calls = [
        _captured_tool("dup", "exec_command", {"command": "echo first"}),
        _captured_tool("dup", "fs_read_file", {"path": "other.txt"}),
        _captured_tool("read", "fs_read_file", {"path": "wanted.txt"}),
    ]
    if leading_invalid:
        calls.insert(0, _captured_tool("dup", "   ", {}))
    response = _captured_response(
        "repeated-call-id",
        "provider/served",
        calls,
        {"prompt_tokens": 22, "completion_tokens": 3},
    )
    trajectory, context, _ = _run_scripted_trajectory(
        tmp_path,
        monkeypatch,
        [response, FINISH],
        **{"agent.stop_on_no_tool_response": True},
    )

    first, _ = [step for step in trajectory.steps if step.source == "agent"]
    ids = [call.tool_call_id for call in first.tool_calls]
    assert len(set(ids)) == 3
    assert {
        item.source_call_id: json.loads(item.content)["tool_name"]
        for item in first.observation.results
    } == {
        ids[0]: "exec_command",
        ids[1]: "fs_read_file",
        "read": "fs_read_file",
    }
    assert [call.arguments for call in first.tool_calls] == [
        {"command": "echo first"},
        {"path": "other.txt"},
        {"path": "wanted.txt"},
    ]
    assert first.metrics.prompt_tokens == 22
    assert first.metrics.completion_tokens == 3
    assert (
        trajectory.final_metrics.total_prompt_tokens == context.n_input_tokens
    )


def test_repaired_call_ids_stay_distinct_when_a_response_id_is_reused(
    tmp_path, monkeypatch
):
    from gc_testkit.scripted_model import FINISH

    def response_for(command):
        return _captured_response(
            "reused-response-id",
            "provider/served",
            [
                _captured_tool("dup", "fs_read_file", {"path": "demo.txt"}),
                _captured_tool("dup", "exec_command", {"command": command}),
            ],
            {"prompt_tokens": 22, "completion_tokens": 3},
        )

    trajectory, _, _ = _run_scripted_trajectory(
        tmp_path,
        monkeypatch,
        [response_for("echo one"), response_for("echo two"), FINISH],
    )

    first, second, _ = [
        step for step in trajectory.steps if step.source == "agent"
    ]
    repaired_first = first.tool_calls[1].tool_call_id
    repaired_second = second.tool_calls[1].tool_call_id
    assert repaired_first != repaired_second
    assert [result.source_call_id for result in first.observation.results] == [
        "dup",
        repaired_first,
    ]
    assert [
        result.source_call_id for result in second.observation.results
    ] == [
        "dup",
        repaired_second,
    ]


def test_responses_api_replays_repaired_tool_call_pairs(tmp_path, monkeypatch):
    from gc_testkit.scripted_model import FINISH

    calls = [
        _captured_tool("dup", "exec_command", {"command": "echo first"}),
        _captured_tool("dup", "fs_read_file", {"path": "other.txt"}),
    ]

    def completed(response_id, tool_calls):
        return {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "model": "provider/served",
                "output": [
                    {
                        "type": "function_call",
                        "id": f"fc_{response_id}_{index}",
                        "call_id": call["id"],
                        "name": call["function"]["name"],
                        "arguments": call["function"]["arguments"],
                    }
                    for index, call in enumerate(tool_calls)
                ],
                "usage": {
                    "input_tokens": 22,
                    "output_tokens": 3,
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        }

    trajectory, _, requests = _run_scripted_trajectory(
        tmp_path,
        monkeypatch,
        [
            completed("response-with-duplicate", calls),
            completed(
                "response-with-finish",
                FINISH["choices"][0]["message"]["tool_calls"],
            ),
        ],
        **{
            "backend.api_mode": "responses",
            "backend.completions_url": "http://test/v1/responses",
        },
    )

    step, final = [step for step in trajectory.steps if step.source == "agent"]
    ids = [call.tool_call_id for call in step.tool_calls]
    assert len(set(ids)) == 2
    assert [call.function_name for call in step.tool_calls] == [
        "exec_command",
        "fs_read_file",
    ]
    assert [item.source_call_id for item in step.observation.results] == ids
    assert (
        json.loads(step.observation.results[0].content)["tool_name"]
        == "exec_command"
    )
    assert step.metrics.prompt_tokens == 22
    assert step.metrics.completion_tokens == 3
    assert step.metrics.extra == {"reasoning_tokens": 0}
    assert final.observation.results[0].source_call_id == "finish"
    assert trajectory.final_metrics.extra == {"reasoning_tokens": 0}

    assert requests[1]["previous_response_id"] is None
    for kind in ("function_call", "function_call_output"):
        assert [
            message["call_id"]
            for message in requests[1]["input"]
            if message.get("type") == kind
        ] == ids
