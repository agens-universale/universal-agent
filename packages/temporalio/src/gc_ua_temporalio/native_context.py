"""Assemble the real core RunContext with workflow-safe external boundaries."""

from contextlib import nullcontext
from copy import deepcopy

# Load these deterministic core modules during sandbox initialization, rather
# than on the first late-bound store/tool dispatch in a workflow task.
from gc_core import thread_store_async as _thread_store_async  # noqa: F401
from gc_core.context_preparation import RunContextBindings
from gc_core.execution import tool_effects as _tool_effects  # noqa: F401
from gc_core.execution.contracts import ExecutionFailure
from gc_core.intent_guard import (
    final_report_consistency as _final_report_consistency,  # noqa: F401
)
from gc_core.model_router import ModelRouter
from gc_core.run_context import assemble_run_context
from gc_core.tools.connect_mcp.tool_wrapper import MCPToolWrapper
from gc_core.tools.registry import get_builtin_tools_data
from gc_core.tools.skills.skills_tools import get_skills_tools_data

from gc_ua_temporalio.native_child_preparation import (
    prepared_subagent_factories,
)

# Import actual tool classes during workflow loading. Prepared worker metadata
# supplies their schemas; only class identity is retained here.
_BUILTIN_TOOL_TYPES = {
    name: entry["tool_fabric"]
    for name, entry in get_builtin_tools_data(
        {"backend_api_mode": "chat_completions"}
    ).items()
}


class ExternalResources:
    """Fail loudly if core tries to bypass an explicit external operation."""

    def __getattr__(self, name):
        raise ExecutionFailure(
            f"Resource {name} requires an external operation"
        )


def assemble_native_context(
    prepared, *, operations, store, logger, human_request
):
    resources = ExternalResources()
    config = prepared.context.config_dict
    candidates = {
        name: {"tool_fabric": cls} for name, cls in _BUILTIN_TOOL_TYPES.items()
    }
    candidates.update(get_skills_tools_data(resources))
    candidates.update(prepared_subagent_factories(prepared))
    tools = {}
    for name, metadata in prepared.tool_metadata.items():
        if metadata.get("execution_kind") == "mcp":
            candidates[name] = {
                "tool_fabric": lambda name=name, **_: MCPToolWrapper(
                    name, None, None, timeout=0, default_use_retry=False
                )
            }
        if name not in candidates:
            raise ExecutionFailure(f"No prepared native tool factory: {name}")
        tools[name] = {
            **deepcopy(metadata),
            "tool_fabric": candidates[name]["tool_fabric"],
        }
    router = ModelRouter(
        config, level_provider=lambda: config.get("level") or "default"
    )
    operations.router = router
    ctx = assemble_run_context(
        prepared.context,
        RunContextBindings(
            os_backend=resources,
            skills_registry=resources,
            logger=logger,
            tools_data=tools,
            model_router=router,
            thread_store=store,
            operations=operations,
            lock_factory=nullcontext,
            prompt_loader=operations.prompt_loader,
            state_preparer=operations.state_preparer,
            human_request=human_request,
            guard_probe=operations.guard_probe,
            tool_effect=operations.tool_effect,
            skills_loader=operations.skills_loader,
            mcp_cleanup=operations.mcp_cleanup,
            prompt_resource=operations.prompt_resource,
            summary_trace_sink=operations.summary_trace_sink,
            project_memory_io=operations.project_memory_io,
            project_memory_hash=operations.project_memory_hash,
            collect_garbage=lambda: None,
        ),
    )

    operations.router_provider = lambda: ctx.model_router
    ctx.model_transport_configured = prepared.model_transport_configured
    ctx.provider_currency = prepared.provider_currency
    return ctx
