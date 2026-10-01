import json
import subprocess
import sys

import pytest
from gc_core.execution.contracts import ToolInvocation
from gc_ua_temporalio.config import AgentProfile, TemporalRuntimeConfig
from gc_ua_temporalio.contracts import ToolActivityInput
from gc_ua_temporalio.runtime import WorkerRuntime
from pydantic import ValidationError


def config(tmp_path, **profiles):
    return TemporalRuntimeConfig(
        database=str(tmp_path / "db.sqlite"),
        artifacts=str(tmp_path / "artifacts"),
        profiles=profiles,
    )


def profile(path, **kwargs):
    return AgentProfile(
        workspace=str(path),
        model="test",
        completions_url="http://localhost:1",
        **kwargs,
    )


async def test_workspaces_and_memory_are_isolated_without_chdir(tmp_path):
    import asyncio

    for name in ("one", "two"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "same.txt").write_text(name)
    runtime = WorkerRuntime(
        config(
            tmp_path,
            one=profile(tmp_path / "one"),
            two=profile(tmp_path / "two"),
        )
    )

    async def read(name):
        return await runtime.tool(
            ToolActivityInput(
                name,
                "v1",
                name,
                ToolInvocation(
                    "read-1", "fs_read_file", {"path": "same.txt"}, "read"
                ),
            )
        )

    results = await asyncio.gather(read("one"), read("two"))
    assert "one" in json.loads(results[0].output)["content"]
    assert "two" in json.loads(results[1].output)["content"]
    outside = await runtime.tool(
        ToolActivityInput(
            "one",
            "v1",
            "one",
            ToolInvocation(
                "read-2", "fs_read_file", {"path": "../two/same.txt"}, "read"
            ),
        )
    )
    assert json.loads(outside.output)["ok"] is False


async def test_large_output_can_be_paged_after_runtime_restart(tmp_path):
    text = "large unicode result: Пример\n" * 180
    (tmp_path / "large.txt").write_text(text)
    cfg = config(tmp_path, demo=profile(tmp_path, inline_output_chars=512))
    runtime = WorkerRuntime(cfg)
    result = await runtime.tool(
        ToolActivityInput(
            "demo",
            "v1",
            "owner",
            ToolInvocation(
                "read", "fs_read_file", {"path": "large.txt"}, "read"
            ),
        )
    )
    assert result.artifacts
    page = json.loads(result.output)
    assert page["stored"] is True
    collected = page["content"]
    restarted = WorkerRuntime(cfg)
    while page["truncated"]:
        next_page = await restarted.tool(
            ToolActivityInput(
                "demo",
                "v1",
                "owner",
                ToolInvocation(
                    f"page-{page['next_offset']}",
                    "session_read_stored_tool_output",
                    {
                        "output_ref": page["output_ref"],
                        "offset": page["next_offset"],
                        "limit": 512,
                    },
                    "page",
                ),
            )
        )
        page = json.loads(next_page.output)
        assert page["ok"], page
        collected += page["content"]
    original = json.loads(collected)
    assert original["ok"]
    assert original["content"].count("Пример") == 180


def test_unsupported_configuration_is_not_silently_disabled(tmp_path):
    for kwargs in (
        {"tools": ["exec_command"]},
        {"strict_turn_finish_validation": True},
        {"text_answer_mode": "judge"},
        {"mcp_servers": {"remote": "sse"}},
    ):
        with pytest.raises(ValidationError):
            profile(tmp_path, **kwargs)


def test_workflow_import_has_no_tool_network_or_filesystem_runtime():
    source = """
import sys
import gc_ua_temporalio.workflow
for name in ("httpx", "gc_core.tools", "gc_core.backends", "gc_core.states", "gc_core.processing_responses", "gc_core.run_context"):
    assert name not in sys.modules, name
"""
    subprocess.run([sys.executable, "-c", source], check=True)


async def test_imported_thread_restores_history_memory_and_todo(tmp_path):
    from gc_core.execution.state import ExecutionState
    from gc_testkit.memory_thread_store import MemoryThreadStore
    from gc_ua_temporalio.contracts import BootstrapRequest, SessionStart
    from gc_ua_temporalio.storage.import_thread import import_stopped_thread

    source = MemoryThreadStore()
    messages = [{"id": "old", "role": "user", "content": "previous turn"}]
    todo = [{"content": "continue", "status": "in_progress"}]
    source.append_messages("temporal", "imported", messages)
    source.save_meta(
        "temporal",
        "imported",
        {
            "shared": {
                "working_memory": {"goal": "resume"},
                "session_todo": todo,
            },
            "user_turn_seq": 3,
            "last_main_prompt_tokens": 77,
            "last_main_prompt_message_count": 4,
        },
    )
    (tmp_path / "AGENTS.md").write_text("Always report the result in Russian.")
    runtime = WorkerRuntime(config(tmp_path, demo=profile(tmp_path)))
    await import_stopped_thread(
        source, runtime.store, "temporal", "imported", writers_stopped=True
    )
    start = SessionStart("imported", "continue")
    with pytest.raises(ValueError, match="explicitly resume"):
        await runtime.bootstrap(BootstrapRequest(start, "owner"))
    start.resume_imported = True
    snapshot = await runtime.bootstrap(BootstrapRequest(start, "owner"))
    restored = ExecutionState.from_execution_snapshot(snapshot.state)
    assert snapshot.history == messages
    assert restored.messages[-1] == messages[-1]
    assert restored.shared.working_memory["goal"] == "resume"
    assert restored.session_todo == todo
    assert restored.user_turn_seq == 3
    assert restored.last_main_prompt_tokens == 77
    assert restored.last_main_prompt_message_count == 4
    assert (
        "Always report the result in Russian."
        in restored.messages[0]["content"]
    )


def test_oversized_model_result_is_rejected_before_activity_completion():
    from gc_core.execution.contracts import ExecutionFailure, ModelResult
    from gc_ua_temporalio.wire import MAX_PAYLOAD_BYTES, validate_payload

    with pytest.raises(ExecutionFailure, match="payload exceeds"):
        validate_payload(ModelResult(text="я" * MAX_PAYLOAD_BYTES))
