"""One registered tool implementation across local and explicit effect ports."""

import json
from types import SimpleNamespace

import pytest
from gc_core.execution.tool_effects import execute_native_tool_effect
from gc_core.security.guard_probe import evaluate_guard_probe
from gc_core.security.permission_store import PermissionStore
from gc_core.tools import aapply_tool
from gc_core.tools.exec.download_file import DownloadFile
from gc_core.tools.fs.write_to_file_tool import WriteToFile
from gc_testkit.helpers.tool_ctx import local_ctx


def setup_ports(config, tool_class):
    worker = local_ctx(config)
    core = local_ctx(config)
    name = tool_class.registered_tool_name()
    registry = {
        name: {
            "tool_fabric": tool_class,
            "spec": tool_class.tool_description(),
        }
    }
    worker.tools_data = core.tools_data = registry
    core.permission_store = PermissionStore()
    events = []

    async def probe(request):
        events.append(("probe", request.kind))
        return evaluate_guard_probe(request, config_dict=config)

    async def human(request):
        events.append(("human", request.decision_key))
        return "allow_once"

    async def effect(request):
        events.append(("effect", request.effect_step))
        return await execute_native_tool_effect(request, ctx=worker)

    core.guard_probe, core.human_request, core.tool_effect = (
        probe,
        human,
        effect,
    )
    return core, worker, events


@pytest.mark.asyncio
async def test_write_runs_guards_and_human_before_leaf(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    dest = tmp_path / "outside.txt"
    ctx, _, events = setup_ports(
        {
            "launch_root": str(project),
            "write_guard": {"enabled": True, "mode": "ask"},
        },
        WriteToFile,
    )
    output = await aapply_tool(
        "fs_write_file",
        {"path": str(dest), "content": "hello"},
        ctx=ctx,
        call_id="write-1",
    )
    assert json.loads(output)["ok"] is True
    assert dest.read_text() == "hello"
    assert events[-2:] == [("human", f"write:{dest}"), ("effect", "body")]
    assert ctx.permission_store.session_rules() == {}


@pytest.mark.asyncio
async def test_changed_guard_facts_prevent_effect(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    dest = project / "file.txt"
    ctx, worker, _ = setup_ports(
        {"launch_root": str(project), "write_guard": {"enabled": True}},
        WriteToFile,
    )

    async def effect(request):
        request.arguments["path"] = str(tmp_path / "other.txt")
        return await execute_native_tool_effect(request, ctx=worker)

    ctx.tool_effect = effect
    # Как в pydantic-ai: изменившиеся факты — ошибка инструмента для модели,
    # а не сбой хода; эффект при этом не выполняется.
    output = await aapply_tool(
        "fs_write_file", {"path": str(dest), "content": "hello"}, ctx=ctx
    )
    assert "Tool arguments changed after guard evaluation" in output
    assert not dest.exists()


@pytest.mark.asyncio
async def test_download_uses_native_human_before_download_effect(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    ctx, _, events = setup_ports(
        {
            "launch_root": str(project),
            "write_guard": {"enabled": True, "mode": "ask"},
            "download": {"download_dir": str(tmp_path / "outside")},
        },
        DownloadFile,
    )

    async def http_get(_self, url, _max_bytes):
        events.append(("http", url))
        return b"%PDF-1.4 fake", None

    monkeypatch.setattr(DownloadFile, "_http_get_bytes", http_get)
    output = await aapply_tool(
        "download_file",
        {"url": "https://example.org/a.pdf", "filename": "a.pdf"},
        ctx=ctx,
    )
    assert json.loads(output)["ok"] is True
    assert (tmp_path / "outside/a.pdf").read_bytes() == b"%PDF-1.4 fake"
    assert [event[0] for event in events][-3:] == ["human", "effect", "http"]
    assert ("effect", "download") in events


@pytest.mark.asyncio
async def test_command_judge_human_and_effect_are_separate(
    tmp_path, monkeypatch
):
    import logging

    import gc_core.security.command_moderation as moderation
    from gc_core.tools.exec.execute_command import ExecuteCommand

    ctx, _, events = setup_ports(
        {
            "launch_root": str(tmp_path),
            "completions_url": "https://model.invalid",
            "command_moderation": {"enabled": True, "mode": "ask"},
        },
        ExecuteCommand,
    )
    from gc_core.config_modules.chat_completions import (
        auxiliary_llm_from_config_dict,
    )

    ctx.auxiliary_llm = auxiliary_llm_from_config_dict(ctx.config_dict)
    ctx.logger = logging.getLogger("native_effect_test")

    async def model(**kwargs):
        events.append(("model", kwargs["consumer"]))
        return json.dumps(
            {
                "verdict": "potentially_dangerous",
                "reasons": ["confirm"],
                "risk_flags": ["confirm"],
                "confidence": 0.9,
            }
        )

    monkeypatch.setattr(
        moderation, "arequest_auxiliary_completion_text", model
    )
    output = await aapply_tool(
        "exec_command",
        {"command": "echo native", "cwd": str(tmp_path)},
        ctx=ctx,
        state=SimpleNamespace(shared=SimpleNamespace()),
    )
    assert json.loads(output)["ok"] is True
    phases = [event[0] for event in events if event[0] != "probe"]
    assert phases == ["model", "human", "effect"]
    assert ("probe", "command_prompts") in events


@pytest.mark.asyncio
async def test_fetch_cache_hit_preserves_lru_order_across_effect(tmp_path):
    from collections import OrderedDict

    from gc_core.tools.exec.simple_fetch_url import SimpleFetchUrl

    ctx, _, events = setup_ports(
        {"launch_root": str(tmp_path)}, SimpleFetchUrl
    )
    state = SimpleNamespace(
        shared=SimpleNamespace(
            fetch_url_cache=OrderedDict(
                [
                    ("https://a.example", {"content": "A"}),
                    ("https://b.example", {"content": "B"}),
                ]
            )
        )
    )
    output = await aapply_tool(
        "exec_fetch_url", {"url": "https://a.example"}, ctx=ctx, state=state
    )
    assert json.loads(output)["cache_hit"] is True
    assert list(state.shared.fetch_url_cache) == [
        "https://b.example",
        "https://a.example",
    ]


@pytest.mark.asyncio
async def test_stored_output_reads_live_pure_store_in_core(tmp_path):
    from gc_core.execution.output_store import (
        ToolCallOutputRecord,
        ToolCallOutputsStore,
    )
    from gc_core.tools.session.stored_outputs.read_stored_tool_output import (
        ReadStoredToolOutput,
    )

    ctx, _, events = setup_ports(
        {"launch_root": str(tmp_path)}, ReadStoredToolOutput
    )
    store = ToolCallOutputsStore()
    store.put(ToolCallOutputRecord("ref1", "source", "call1", 5, "hello"))
    state = SimpleNamespace(tool_call_outputs_store=store)
    output = await aapply_tool(
        "session_read_stored_tool_output",
        {"output_ref": "ref1"},
        ctx=ctx,
        state=state,
    )
    assert json.loads(output)["content"] == "hello"
    assert events == []
