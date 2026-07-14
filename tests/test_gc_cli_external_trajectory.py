"""Tests for the ATIF trajectory mapping in the Harbor external adapter.

Requires the `harbor` framework (dependency-group `test`).
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# tests/harbor/ (a test-fixture package) shadows the real harbor framework:
# reorder sys.path so real site-packages wins, then evict cached shadows.
_harbor_sp = next(
    (p for p in sys.path
     if (Path(p) / "harbor" / "agents" / "base.py").exists()),
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
_HARBOR = Path(__file__).resolve().parent.parent / "harbor"
sys.path.insert(0, str(_HARBOR))

from adapters.gc_cli_external import (  # noqa: E402
    GcCliExternal,
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


def test_gc_cli_config_overrides_merge_into_build_config():
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
    cfg = agent._build_config()
    # scalar override wins
    assert cfg["no_tool_call_max_count"] == 5
    # nested override merges into the structural defaults: temperature is added,
    # tool_choice is kept; no hardcoded sampling default is shadowing it.
    assert cfg["chat_completion_kwargs"]["temperature"] == 0.7
    assert cfg["chat_completion_kwargs"]["tool_choice"] == "auto"
    # adapter ships NO sampling defaults (generation_config governs by default)
    assert "top_p" not in cfg["chat_completion_kwargs"]
    assert "top_k" not in cfg["chat_completion_kwargs"]


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

    agent = GcCliExternal(logs_dir=tmp_path, model_name="openai/m", session_id="s",
                          gc_cli_config={"completions_url": "http://test/v1/chat/completions"})
    context = SimpleNamespace(metadata=None)

    with pytest.raises(RuntimeError, match="HTTPError"):
        asyncio.run(agent.run("do X", environment=object(), context=context))

    traj = tmp_path / "trajectory.json"
    assert traj.exists()  # partial trace flushed despite the abort
    assert context.metadata["total_steps"] == 2


def test_runtime_url_and_model_beat_profile_overrides(monkeypatch):
    from pathlib import Path

    # Runtime sources (runner --model / env URL) are authoritative over profile.
    monkeypatch.setenv("GC_CLI_BACKEND_URL", "http://runtime/v1/chat/completions")
    agent = GcCliExternal(
        logs_dir=Path("/tmp"),
        model_name="openai/real-model",
        gc_cli_config={"completions_url": "http://stale", "model": "stale"},
    )
    cfg = agent._build_config()
    assert cfg["model"] == "real-model"  # openai/ stripped, profile value ignored
    assert cfg["completions_url"] == "http://runtime/v1/chat/completions"
