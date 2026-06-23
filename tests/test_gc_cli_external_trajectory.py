"""Tests for the ATIF trajectory mapping in the Harbor external adapter.

The adapter imports `harbor`, which lives in harbor's own tool venv — not the
default test env. Skip cleanly where harbor is absent.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("harbor")

# The adapter is not part of src/; load it from harbor/adapters/.
_ADAPTERS = Path(__file__).resolve().parent.parent / "harbor" / "adapters"
sys.path.insert(0, str(_ADAPTERS))

from gc_cli_external import (  # noqa: E402
    GcCliExternal,
    _deep_merge,
    _messages_to_steps,
    _stringify_content,
)


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


def test_stringify_handles_none_str_and_parts():
    assert _stringify_content(None) == ""
    assert _stringify_content("x") == "x"
    assert (
        _stringify_content([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
        == "a\nb"
    )


def test_deep_merge_overlays_nested_dicts():
    base = {"a": 1, "nested": {"x": 1, "y": 2}}
    over = {"a": 9, "nested": {"y": 20, "z": 3}}
    assert _deep_merge(base, over) == {"a": 9, "nested": {"x": 1, "y": 20, "z": 3}}


def test_gc_cli_config_overrides_merge_into_build_config():
    from pathlib import Path

    agent = GcCliExternal(
        logs_dir=Path("/tmp"),
        model_name="openai/m",
        session_id="s",
        gc_cli_config={
            "no_tool_call_max_count": 5,
            "chat_completion_kwargs": {"temperature": 0.7},
        },
    )
    cfg = agent._build_config()
    # scalar override wins
    assert cfg["no_tool_call_max_count"] == 5
    # nested override merges (temperature replaced, top_p kept from defaults)
    assert cfg["chat_completion_kwargs"]["temperature"] == 0.7
    assert cfg["chat_completion_kwargs"]["top_p"] == 0.95


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

        def run(self, messages):
            raise RuntimeError("HTTPError from backend")  # mimics the abort

        def get_state(self):
            return partial_state

    monkeypatch.setattr("run_context.create_run_context", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr("agent_executor.AgentExecutor", FakeExecutor)

    agent = GcCliExternal(logs_dir=tmp_path, model_name="openai/m", session_id="s")
    context = SimpleNamespace(metadata=None)

    with pytest.raises(RuntimeError, match="HTTPError"):
        asyncio.run(agent.run("do X", environment=object(), context=context))

    traj = tmp_path / "trajectory.json"
    assert traj.exists()  # partial trace flushed despite the abort
    assert context.metadata["total_steps"] == 2


def test_runtime_url_and_model_beat_profile_overrides():
    from pathlib import Path

    agent = GcCliExternal(
        logs_dir=Path("/tmp"),
        model_name="openai/real-model",
        gc_cli_config={"completions_url": "http://stale", "model": "stale"},
    )
    cfg = agent._build_config()
    assert cfg["model"] == "real-model"  # openai/ stripped, profile value ignored
    assert cfg["completions_url"] != "http://stale"
