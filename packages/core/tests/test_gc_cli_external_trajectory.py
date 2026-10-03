"""Tests for the ATIF trajectory mapping in the Harbor external adapter.

Requires the `harbor` framework (dependency-group `test`).
"""

import asyncio
import json
import sys
from pathlib import Path
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


def test_trajectory_json_preserves_archived_and_live_reasoning(tmp_path):
    agent = GcCliExternal(logs_dir=tmp_path, model_name="openai/m")
    agent._evicted_messages = [
        {
            "role": "assistant",
            "content": "old",
            "reasoning_content": "old thought",
        },
    ]
    state = SimpleNamespace(
        messages=[
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "new thought",
            },
            {
                "type": "function_call",
                "name": "exec_command",
                "arguments": "{}",
            },
            {"type": "function_call_output", "output": "done"},
        ]
    )
    assert agent._write_trajectory(state) == 3
    payload = json.loads((tmp_path / "trajectory.json").read_text())
    assert payload["steps"][0]["reasoning_content"] == "old thought"
    assert payload["steps"][1]["reasoning_content"] == "new thought"
    assert (
        payload["steps"][2]["observation"]["results"][0]["content"] == "done"
    )


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
            "completions_url": "http://test/v1/chat/completions",
            "no_tool_call_max_count": 5,
            "chat_completion_kwargs": {"temperature": 0.7},
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
        gc_cli_config={"completions_url": "http://test/v1/chat/completions"},
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
        "completions_url", "http://test/v1/chat/completions"
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
                "chat_completion_headers": {"Authorization": "Bearer real-key"}
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
        gc_cli_config={"completions_url": "http://stale", "model": "stale"},
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
    assert _agent()._populate_usage(context, None) is None
    assert context.n_input_tokens is None

    totals = _agent()._populate_usage(context, _state_with_usage())
    assert totals["calls"] == 0
    assert context.n_input_tokens is None


def test_calls_without_usage_are_reported_not_hidden():
    state = _state_with_usage(input_tokens=100, output_tokens=10, missing=3)
    totals = _agent()._populate_usage(AgentContext(), state)
    assert (totals["calls"], totals["missing_usage_calls"]) == (4, 3)


def test_reporting_failure_never_breaks_the_run():
    """It runs inside a finally that may be unwinding a timeout."""

    class Exploding:
        @property
        def usage(self):
            raise RuntimeError("state is half-torn-down")

    assert _agent()._populate_usage(AgentContext(), Exploding()) is None
