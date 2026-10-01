"""Actual tools run against planning/checked resource ports, including recovery."""

import json

import pytest
from gc_core.execution.contracts import ModelResult, ToolInvocation
from gc_ua_temporalio.config import (
    SUPPORTED_TOOLS,
    AgentProfile,
    TemporalRuntimeConfig,
)
from gc_ua_temporalio.contracts import ToolActivityInput
from gc_ua_temporalio.control_contracts import (
    ExecuteEffectInput,
    JudgeDecision,
    PrepareToolInput,
    ReconcileRequest,
)
from gc_ua_temporalio.guarded_tools import GuardedTools
from gc_ua_temporalio.runtime import WorkerRuntime


@pytest.fixture
def guarded(tmp_path):
    (tmp_path / "downloads").mkdir()
    config = TemporalRuntimeConfig(
        database=str(tmp_path / "db"),
        artifacts=str(tmp_path / "artifacts"),
        profiles={
            "demo": AgentProfile(
                workspace=str(tmp_path),
                model="fixture",
                completions_url="http://localhost:1",
                tools=list(SUPPORTED_TOOLS),
            )
        },
    )
    return GuardedTools(WorkerRuntime(config))


def request(name, arguments, operation="effect-1"):
    return PrepareToolInput(
        ToolActivityInput(
            "demo",
            "v1",
            "owner",
            ToolInvocation(operation, name, arguments, "call-1"),
        )
    )


@pytest.mark.parametrize(
    "name,arguments,expected",
    [
        (
            "fs_append_file",
            {"content": "world", "separator": "\n"},
            "hello\nworld",
        ),
        (
            "fs_search_and_replace",
            {"search": "hello", "replace": "world"},
            "world",
        ),
        (
            "fs_apply_file_edits",
            {"edits": [{"search": "hello", "replace": "world"}]},
            "world",
        ),
        ("fs_save_base64_file", {"data": "d29ybGQ="}, "world"),
    ],
)
async def test_file_tools_prepare_without_writing_then_reconcile(
    guarded, tmp_path, name, arguments, expected
):
    path = tmp_path / "sample.txt"
    path.write_text("hello")
    value = request(name, {"path": "sample.txt", **arguments})
    plan = await guarded.prepare(value)
    assert path.read_text() == "hello"
    assert plan.content is not None, plan
    outcome = await guarded.execute(ExecuteEffectInput(value, plan))
    assert (
        outcome.status == "complete"
        and json.loads(outcome.result.output)["ok"]
    ), outcome
    assert path.read_text() == expected
    modified = path.stat().st_mtime_ns
    assert await guarded.reconcile(ExecuteEffectInput(value, plan)) == outcome
    assert path.stat().st_mtime_ns == modified


async def approve_judge(guarded, value):
    plan = await guarded.prepare(value)
    assert plan.judge.consumer == "command_judge", plan
    value.judges.append(
        JudgeDecision(
            plan.judge_digest,
            ModelResult(
                text=json.dumps(
                    {
                        "verdict": "safe",
                        "reasons": ["fixture"],
                        "risk_flags": [],
                    }
                )
            ),
        )
    )
    return await guarded.prepare(value)


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("exec_command", {"command": "echo hello"}),
        ("exec_python", {"code": "print('hello')"}),
    ],
)
async def test_command_judge_is_separate_and_execution_has_receipt(
    guarded, name, arguments
):
    value = request(name, arguments)
    plan = await approve_judge(guarded, value)
    assert plan.effect_kind == "command", plan
    outcome = await guarded.execute(ExecuteEffectInput(value, plan))
    assert outcome.status == "complete", outcome
    assert "hello" in outcome.result.output
    assert json.loads(outcome.result.output)["ok"] is True
    assert await guarded.execute(ExecuteEffectInput(value, plan)) == outcome


async def test_unknown_command_never_reexecutes_and_needs_operator_evidence(
    guarded, tmp_path
):
    value = request(
        "exec_python",
        {
            "code": "from pathlib import Path; Path('counter').write_text('one')"
        },
    )
    plan = await approve_judge(guarded, value)

    async def crash(_):
        raise KeyboardInterrupt("lost receipt")

    guarded.after_effect = crash
    effect = ExecuteEffectInput(value, plan)
    with pytest.raises(KeyboardInterrupt):
        await guarded.execute(effect)
    modified = (tmp_path / "counter").stat().st_mtime_ns
    assert (await guarded.reconcile(effect)).status == "unknown"
    assert (await guarded.execute(effect)).status == "unknown"
    effect.resolution = ReconcileRequest(
        "effect-1",
        "checked-1",
        "applied",
        "counter contains one; process exited",
        True,
    )
    result = await guarded.reconcile(effect)
    assert result.status == "complete"
    assert await guarded.execute(effect) == result
    assert (tmp_path / "counter").stat().st_mtime_ns == modified


async def test_download_uses_existing_validation_and_prepared_bytes(
    guarded, tmp_path, monkeypatch
):
    from gc_core.tools.exec.download_file import DownloadFile

    calls = []

    async def fetch(self, url, max_bytes):  # noqa: ARG001 - transport contract
        calls.append(url)
        return b"downloaded", None

    monkeypatch.setattr(DownloadFile, "_http_get_bytes", fetch)
    value = request(
        "download_file",
        {"url": "https://example.com/file.txt", "filename": "file.txt"},
    )
    plan = await guarded.prepare(value)
    assert plan.content is not None, plan
    assert not (tmp_path / "downloads/file.txt").exists()
    outcome = await guarded.execute(ExecuteEffectInput(value, plan))
    assert outcome.status == "complete", outcome
    assert (tmp_path / "downloads/file.txt").read_text() == "downloaded"
    assert len(calls) == 1
