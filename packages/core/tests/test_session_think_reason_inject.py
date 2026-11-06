"""Acceptance: session_think + inject_tool_fields.reason (spec 03)."""

import json

import pytest
from gc_core.tools import aapply_tool, abuild_tools_data
from gc_testkit.plugin import ensure_real_tools_package
from gc_testkit.settings import fake_ctx, make_settings


@pytest.fixture(autouse=True)
def _ensure_real_tools_module():
    """Keep the real module identity shared by tool calls and teardown."""
    ensure_real_tools_package()


def _inject_config(**overrides):
    cfg = {
        "agent.use_local_subagents": False,
        "backend.api_mode": "chat_completions",
        "paths.skills_data_path": "",
        "agent.stop_tools": [],
        "agent.inject_tool_fields": {
            "reason": "Brief rationale (1–2 sentences): goal, why now, expected outcome."
        },
    }
    cfg.update(overrides)
    return cfg


async def test_apply_tool_session_think_reason_only_ok_with_inject():
    """session_think({reason: long text}) with inject → ok; content not lost after strip."""
    tools_data, _, _, _ = await abuild_tools_data(
        make_settings(_inject_config())
    )
    ctx = fake_ctx(
        _inject_config(),
        tools_data=tools_data,
        logger=None,
    )
    raw = await aapply_tool(
        tool_name="session_think",
        arguments=json.dumps(
            {"reason": "Need to weigh trade-offs before editing the parser."}
        ),
        ctx=ctx,
    )
    data = json.loads(raw)
    assert data["ok"] is True
    assert data.get("error") is None


async def test_apply_tool_session_think_explicit_thoughts_unchanged():
    tools_data, _, _, _ = await abuild_tools_data(
        make_settings(_inject_config())
    )
    ctx = fake_ctx(
        _inject_config(),
        tools_data=tools_data,
        logger=None,
    )
    raw = await aapply_tool(
        tool_name="session_think",
        arguments=json.dumps(
            {
                "reason": "why call",
                "thoughts": "explicit structured thoughts",
            }
        ),
        ctx=ctx,
    )
    data = json.loads(raw)
    assert data["ok"] is True
    assert data.get("error") is None


async def test_apply_tool_session_think_empty_after_strip_still_fails():
    tools_data, _, _, _ = await abuild_tools_data(
        make_settings(_inject_config())
    )
    ctx = fake_ctx(
        _inject_config(),
        tools_data=tools_data,
        logger=None,
    )
    raw = await aapply_tool(
        tool_name="session_think",
        arguments=json.dumps({"reason": "   "}),
        ctx=ctx,
    )
    data = json.loads(raw)
    assert data["ok"] is False
    assert data["error"]["type"] == "invalid_argument"
