"""Generated child roles use a separate model operation and inherit remaining budgets."""

from gc_core.execution.contracts import ModelResult, ToolInvocation
from gc_ua_temporalio.activities import AgentActivities
from gc_ua_temporalio.child_activities import ChildActivities
from gc_ua_temporalio.child_contracts import PrepareChild
from gc_ua_temporalio.config import AgentProfile, TemporalRuntimeConfig
from gc_ua_temporalio.contracts import (
    BootstrapRequest,
    SessionStart,
    ToolActivityInput,
)
from gc_ua_temporalio.control_contracts import JudgeDecision
from gc_ua_temporalio.runtime import WorkerRuntime


async def test_generated_role_is_replayed_from_model_result_and_uses_child_profile(
    tmp_path,
):
    base = AgentProfile(
        workspace=str(tmp_path),
        model="parent",
        completions_url="http://127.0.0.1:1",
        max_tool_calls=5,
    )
    runtime = WorkerRuntime(
        TemporalRuntimeConfig(
            database=str(tmp_path / "db"),
            artifacts=str(tmp_path / "artifacts"),
            profiles={
                "demo": base.model_copy(
                    update={"subagents": {"subagent_special_agent": "child"}}
                ),
                "child": base.model_copy(
                    update={"model": "child-model", "max_tool_calls": 20}
                ),
            },
        )
    )
    start = SessionStart("role-test", "delegate")
    snapshot = await runtime.bootstrap(BootstrapRequest(start, "owner"))
    snapshot.state["data"]["tool_calls"] = 3
    tool = ToolActivityInput(
        "demo",
        "v1",
        "owner",
        ToolInvocation(
            "role-1",
            "subagent_special_agent",
            {
                "task": "Review the result",
                "reason": "Need reviewer",
                "role": "Reviewer",
                "role_description": "Check arithmetic",
            },
            "call-1",
        ),
    )
    data = PrepareChild("owner", tool, start, snapshot.state)
    activities = ChildActivities(AgentActivities(runtime))
    plan = await activities._prepare(data)
    assert plan.judge is not None
    assert plan.model_profile == "child"
    assert plan.judge.state["default_model"] == "child-model"
    data.judges.append(
        JudgeDecision(
            plan.judge_digest,
            ModelResult(text="name: reviewer\ndescription: Review arithmetic"),
        )
    )
    resolved = await activities._prepare(data)
    assert resolved.judge is None and resolved.start.profile == "child"
    assert "Review arithmetic" in resolved.start.child_seed["role"]
    assert resolved.start.child_seed["max_tool_calls"] == 2
