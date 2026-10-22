"""Resource dispatch preserves the core decisions without local I/O fallback."""

import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from gc_core.config_modules.root import AppConfig
from gc_core.context_preparation import PreparedRunContext, RunContextBindings
from gc_core.execution.contracts import InfrastructureFailure
from gc_core.execution.resources import (
    PromptResourceRequest,
    aload_prompt_resource,
)
from gc_core.model_router import ModelRouter
from gc_core.project_memory import compaction
from gc_core.project_memory.operations import ProjectMemoryReply
from gc_core.run_context import assemble_run_context
from gc_core.summarize.summary_logging import aappend_summary_trace_record
from gc_core.tools.project.project_memory import ProjectMemory
from gc_testkit.settings import fake_ctx


def forbidden(*_args, **_kwargs):
    raise AssertionError("Unexpected local resource access")


@pytest.mark.parametrize(
    "force_legacy,bound,available",
    [
        (False, False, True),
        (True, False, False),
        (False, True, False),
    ],
)
def test_context_assembly_only_consumes_prepared_values(
    monkeypatch, force_legacy, bound, available
):
    prepared = PreparedRunContext(
        settings={
            "agent": {"model": "test"},
            "backend": {"api_mode": "chat_completions"},
            "models": {"fast": {}, "mid": {}, "big": {}},
            "slots": {"light": "fast", "default": "mid", "max": "big"},
            "level": "auto",
        },
        runtime={},
        run_id="recorded-run",
        logging_path="/worker/log",
        summary_trace_path="",
        trace_id="trace",
        project_gc_dir="/worker/project/.gc",
        workspace_root="/worker/project",
        workspace_shell="bash",
        workspace_os_name="Linux",
        workspace_root_writable=True,
        tools_spec=[],
        tools_description=[],
        mcp_status={},
        registered_tool_names=["project_memory"],
        mcp_session_owner="owner",
    )
    router = ModelRouter(
        AppConfig.model_validate(prepared.settings),
        level_provider=lambda: "auto",
        force_legacy=force_legacy,
    )
    if bound:
        router = router.with_override(router.resolve_level("default"))
    port = object()
    bindings = RunContextBindings(
        os_backend=port,
        skills_registry=port,
        logger=port,
        tools_data={},
        model_router=router,
        thread_store=port,
        prompt_resource=port,
        project_memory_io=port,
        lock_factory=port,
    )
    with monkeypatch.context() as guarded:
        guarded.setattr("builtins.open", forbidden)
        guarded.setattr("os.getcwd", forbidden)
        guarded.setattr("uuid.uuid4", forbidden)
        guarded.setattr("pathlib.Path.resolve", forbidden)
        ctx = assemble_run_context(prepared, bindings)
    assert ctx.workspace.root == "/worker/project"
    assert ctx.prompt_resource is ctx.thread_store is ctx.os_backend is port
    assert ctx.project_memory_io is ctx.lock_factory is port
    assert ctx.registered_tool_names == {"project_memory"}
    assert ctx.settings.agent.model == "test"
    assert ctx.settings.backend.api_mode == "chat_completions"
    assert ctx.auto_available is available
    assert ctx.model_router.force_legacy is force_legacy
    assert ctx.model_router.auto_allowed is (not bound)


async def test_resource_port_failure_never_falls_back_to_files():
    failure = InfrastructureFailure("activity unavailable")
    ctx = SimpleNamespace(prompt_resource=AsyncMock(side_effect=failure))
    with pytest.raises(InfrastructureFailure) as caught:
        await aload_prompt_resource(
            ctx, PromptResourceRequest("summary"), local=forbidden
        )
    assert caught.value is failure


async def test_summary_trace_sink_receives_data_without_resolving_local_path(
    monkeypatch,
):
    sink = AsyncMock()
    ctx = fake_ctx({"logging.save_logs": True}, summary_trace_sink=sink)
    monkeypatch.setattr("os.getcwd", forbidden)
    monkeypatch.setattr("builtins.open", forbidden)
    await aappend_summary_trace_record(
        ctx,
        trigger_reason="turn_end",
        trace_data={"batches": 2},
        newly_summarized_message_id="summary-1",
    )
    assert asdict(sink.call_args.args[0]) == {
        "trigger_reason": "turn_end",
        "trace_data": {"batches": 2},
        "newly_summarized_message_id": "summary-1",
        "schema_version": 1,
    }


async def test_project_memory_compaction_is_between_file_phases(monkeypatch):
    events = []
    state = SimpleNamespace(project_memory_content_hash="before")

    async def io(request):
        events.append(request.phase)
        if request.phase == "prepare":
            return ProjectMemoryReply(None, combined="existing\n\nnew")
        assert request.compaction.text == "compact"
        assert state.project_memory_content_hash == "before"
        return ProjectMemoryReply(
            '{"ok":true}', baseline_updated=True, baseline_hash="after"
        )

    async def compact(text, _ctx, live_state):
        assert live_state is state
        assert text == "existing\n\nnew"
        events.append("model")
        return compaction.CompactionResult(True, "compact", None, None)

    monkeypatch.setattr(compaction, "acompact_text", compact)
    monkeypatch.setattr(
        "gc_core.tools.project.project_memory.backup_path", forbidden
    )
    monkeypatch.setattr(
        "gc_core.tools.project.project_memory.storage.read", forbidden
    )
    monkeypatch.setattr(
        "gc_core.tools.project.project_memory.storage.stat", forbidden
    )
    ctx = fake_ctx(project_gc_dir="/worker/.gc", project_memory_io=io)
    result = await ProjectMemory(ctx=ctx, state=state).acall("append", "new")
    assert json.loads(result)["ok"]
    assert events == ["prepare", "model", "finish"]
    assert state.project_memory_content_hash == "after"


async def test_failed_project_memory_write_does_not_advance_baseline():
    failure = InfrastructureFailure("write receipt missing")
    ctx = fake_ctx(
        project_gc_dir="/worker/.gc",
        project_memory_io=AsyncMock(side_effect=failure),
    )
    state = SimpleNamespace(project_memory_content_hash="before")
    with pytest.raises(InfrastructureFailure):
        await ProjectMemory(ctx=ctx, state=state).acall("write", "new")
    assert state.project_memory_content_hash == "before"


async def test_durable_trace_write_failure_is_not_swallowed():
    from gc_core.agent_executor import _aappend_trace_id_to_meta

    failure = InfrastructureFailure("store unavailable")
    session = SimpleNamespace(
        store=object(),
        system_id="test",
        thread_id="thread",
        port=SimpleNamespace(aload_meta=AsyncMock(side_effect=failure)),
    )
    with pytest.raises(InfrastructureFailure) as caught:
        await _aappend_trace_id_to_meta(session, "trace")
    assert caught.value is failure


def test_replay_aware_logger_adapter_is_retained():
    import logging

    from gc_core.tools import _logger_for_call

    logger = logging.LoggerAdapter(logging.getLogger("workflow"), {})
    assert _logger_for_call(SimpleNamespace(logger=logger)) is logger


def test_legacy_usage_currency_fact_does_not_require_worker_endpoint():
    from gc_core.usage.account import UsageAccount
    from gc_core.usage.report import build_run_usage_dict

    account = UsageAccount()
    account.add("main", 100, 20, cost=2.5, billed=True)
    pricing = {"enabled": True}
    native = build_run_usage_dict(account, pricing, "m", provider_currency="₽")
    local = build_run_usage_dict(
        account, pricing, "m", "https://provider.ru/api"
    )
    assert native == local
    assert native["cost"]["by_currency"] == {"₽": 2.5}
