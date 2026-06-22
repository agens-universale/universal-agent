"""Tests for the ATIF trajectory mapping in the Harbor external adapter.

The adapter imports `harbor`, which lives in harbor's own tool venv — not the
default test env. Skip cleanly where harbor is absent.
"""
import sys
from pathlib import Path

import pytest

pytest.importorskip("harbor")

# The adapter is not part of src/; load it from harbor/adapters/.
_ADAPTERS = Path(__file__).resolve().parent.parent / "harbor" / "adapters"
sys.path.insert(0, str(_ADAPTERS))

from gc_cli_external import _messages_to_steps, _stringify_content  # noqa: E402


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
