import json

import pytest
from gc_core.execution.contracts import ToolInvocation
from gc_ua_temporalio.config import (
    DEFAULT_TOOLS,
    AgentProfile,
    TemporalRuntimeConfig,
)
from gc_ua_temporalio.contracts import ToolActivityInput
from gc_ua_temporalio.control_contracts import (
    ExecuteEffectInput,
    HumanDecision,
    PrepareToolInput,
)
from gc_ua_temporalio.guarded_tools import GuardedTools
from gc_ua_temporalio.runtime import WorkerRuntime
from gc_ua_temporalio.storage.sqlite import ProjectionConflict


@pytest.fixture
def configuration(tmp_path):
    return TemporalRuntimeConfig(
        database=str(tmp_path / "threads.sqlite"),
        artifacts=str(tmp_path / "artifacts"),
        profiles={
            "demo": AgentProfile(
                workspace=str(tmp_path),
                model="fixture",
                completions_url="http://localhost:1",
                tools=[*DEFAULT_TOOLS, "fs_write_file", "turn_ask_human"],
            )
        },
    )


def write(path="result.txt", content="durable bytes", operation="write-1"):
    return PrepareToolInput(
        ToolActivityInput(
            "demo",
            "v1",
            "owner",
            ToolInvocation(
                operation,
                "fs_write_file",
                {"path": path, "content": content},
                "call-1",
            ),
        )
    )


async def test_dry_run_then_write_and_receipt_do_not_repeat_effect(
    configuration, tmp_path
):
    guarded = GuardedTools(WorkerRuntime(configuration))
    data = write()
    plan = await guarded.prepare(data)
    assert plan.content is not None, plan
    assert not (tmp_path / "result.txt").exists()
    request = ExecuteEffectInput(data, plan)
    result = await guarded.execute(request)
    assert result.status == "complete"
    assert json.loads(result.result.output)["ok"]
    assert (tmp_path / "result.txt").read_text() == "durable bytes"
    modified = (tmp_path / "result.txt").stat().st_mtime_ns
    restarted = GuardedTools(WorkerRuntime(configuration))
    assert await restarted.execute(request) == result
    assert (tmp_path / "result.txt").stat().st_mtime_ns == modified


async def test_approval_is_bound_to_file_facts_and_survives_restart(
    configuration, tmp_path
):
    path = tmp_path / "AGENTS.md"
    path.write_text("original")
    guarded = GuardedTools(WorkerRuntime(configuration))
    data = write("AGENTS.md", "approved")
    first = await guarded.prepare(data)
    assert first.request.kind == "confirm"
    assert path.read_text() == "original"
    data.decisions.append(HumanDecision(first.request_digest, "allow_once"))
    restarted = GuardedTools(WorkerRuntime(configuration))
    allowed = await restarted.prepare(data)
    assert allowed.content is not None and allowed.request is None
    path.write_text("edited while the user was thinking")
    changed = await restarted.prepare(data)
    assert changed.request.kind == "confirm"
    assert changed.request_digest != first.request_digest
    assert (
        await restarted.execute(ExecuteEffectInput(data, allowed))
    ).status == "replan"
    assert path.read_text() == "edited while the user was thinking"


async def test_denied_approval_uses_existing_guard_envelope(
    configuration, tmp_path
):
    (tmp_path / "AGENTS.md").write_text("original")
    guarded = GuardedTools(WorkerRuntime(configuration))
    data = write("AGENTS.md")
    question = await guarded.prepare(data)
    data.decisions.append(HumanDecision(question.request_digest, "deny_once"))
    plan = await guarded.prepare(data)
    assert plan.content is None
    assert json.loads(plan.result.output)["ok"] is False
    assert (tmp_path / "AGENTS.md").read_text() == "original"


async def test_effect_before_receipt_is_reconciled_without_rewriting(
    configuration, tmp_path
):
    class LostAck(GuardedTools):
        async def after_effect(self, data):
            raise KeyboardInterrupt("simulated process loss")

    guarded = LostAck(WorkerRuntime(configuration))
    data = write()
    plan = await guarded.prepare(data)
    request = ExecuteEffectInput(data, plan)
    with pytest.raises(KeyboardInterrupt):
        await guarded.execute(request)
    modified = (tmp_path / "result.txt").stat().st_mtime_ns
    restarted = GuardedTools(WorkerRuntime(configuration))
    assert (await restarted.execute(request)).status == "unknown"
    result = await restarted.reconcile(request)
    assert result.status == "complete" and result.result == plan.result
    assert (tmp_path / "result.txt").stat().st_mtime_ns == modified


async def test_unknown_fences_other_writes_and_late_attempt_is_tombstoned(
    configuration, tmp_path
):
    guarded = GuardedTools(WorkerRuntime(configuration))
    data = write()
    plan = await guarded.prepare(data)
    request = ExecuteEffectInput(data, plan)
    # Cancellation/timeout before the old process reached ledger.begin.
    result = await guarded.reconcile(request)
    assert json.loads(result.result.output)["error"]["type"] == "not_applied"
    assert await guarded.execute(request) == result
    assert not (tmp_path / "result.txt").exists()

    from dataclasses import asdict

    data = write(operation="unresolved")
    plan = await guarded.prepare(data)
    guarded.ledger.begin(
        "owner",
        "unresolved",
        str(tmp_path.resolve()),
        plan.fingerprint,
        asdict(plan),
    )
    (tmp_path / "result.txt").write_text("ambiguous third-party content")
    assert (
        await guarded.reconcile(ExecuteEffectInput(data, plan))
    ).status == "unknown"
    other = write("other.txt", operation="other")
    other_plan = await guarded.prepare(other)
    with pytest.raises(ProjectionConflict, match="unresolved"):
        await guarded.execute(ExecuteEffectInput(other, other_plan))
    assert not (tmp_path / "other.txt").exists()


async def test_human_question_uses_existing_tool_result(configuration):
    guarded = GuardedTools(WorkerRuntime(configuration))
    data = PrepareToolInput(
        ToolActivityInput(
            "demo",
            "v1",
            "owner",
            ToolInvocation(
                "ask-1", "turn_ask_human", {"question": "Какой цвет?"}, "ask"
            ),
        )
    )
    question = await guarded.prepare(data)
    assert question.request.question == "Какой цвет?"
    data.decisions.append(HumanDecision(question.request_digest, "синий"))
    answered = await guarded.prepare(data)
    output = json.loads(answered.result.output)
    assert output["ok"] and output["response"] == "синий"
    assert "hint" in output
