"""Assemble the real core RunContext with workflow-safe external boundaries."""

from contextlib import nullcontext
from copy import deepcopy

from gc_core.agent_store import AgentStore
from gc_core.config_modules.root import AppConfig
from gc_core.context_preparation import (
    RunContextBindings,
    settings_from_snapshot,
)
from gc_core.execution import tool_effects as _tool_effects  # noqa: F401
from gc_core.execution.contracts import ExecutionFailure
from gc_core.intent_guard import (
    final_report_consistency as _final_report_consistency,  # noqa: F401
)
from gc_core.model_router import ModelRouter
from gc_core.run_context import assemble_run_context

# Load these deterministic core modules during sandbox initialization, rather
# than on the first late-bound store/tool dispatch in a workflow task.
from gc_core.threads import session_writer as _session_writer  # noqa: F401
from gc_core.tools.connect_mcp.tool_wrapper import MCPToolWrapper
from gc_core.tools.registry import get_builtin_tools_data
from gc_core.tools.skills.skills_tools import get_skills_tools_data
from temporalio import workflow

from gc_core_temporalio.child_preparation import (
    prepared_subagent_factories,
    seed_prepared_subagent_definitions,
)

# Import actual tool classes during workflow loading. Prepared worker metadata
# supplies their schemas; only class identity is retained here.
_BUILTIN_TOOL_TYPES = {
    name: entry["tool_fabric"]
    for name, entry in get_builtin_tools_data(
        AppConfig.model_validate({"backend": {"api_mode": "chat_completions"}})
    ).items()
}


class ExternalResources:
    """Fail loudly if core tries to bypass an explicit external operation.

    ``is_sandbox`` — данные, а не операция: признак бэкенда воркера из
    подготовленного контекста (координаты путей в отчётах тулов).
    ``workflow_paths`` — пути отчётов считаются без ``os.path`` (от корня
    рабочей области). ``default_cwd()`` — тоже данные: корень рабочей области
    воркера; его спрашивает отчёт отказа охраны команды без ``cwd``.
    """

    workflow_paths = True

    def __init__(self, *, is_sandbox: bool = False, root: str = "") -> None:
        self.is_sandbox = bool(is_sandbox)
        self._root = root or None

    def default_cwd(self):
        return self._root

    def __getattr__(self, name):
        raise ExecutionFailure(
            f"Resource {name} requires an external operation"
        )


class ExternalAgentStore(AgentStore):
    """Хранилище агента в коде workflow: промпты, инструкции, скиллы,
    субагенты и память читаются на воркере (activity), здесь — ошибка."""

    @staticmethod
    def _external(what: str):
        return ExecutionFailure(
            f"Agent resource {what} requires an external operation"
        )

    async def read(self, kind, name, *, project=None):
        del project
        raise self._external(f"{kind}:{name}")

    async def list(self, kind, prefix="", *, project=None):
        del project
        raise self._external(f"{kind}:{prefix}")

    async def read_memory(self, project):
        raise self._external(f"memory:{project.id}")

    async def write_memory(self, project, text, *, expected_version):
        raise self._external(f"memory:{project.id}")


def assemble_context(
    prepared,
    *,
    operations,
    store,
    logger,
    human_request,
    request_equivalence=None,
    require_request_equivalence=False,
    subagent_independent_auto=True,
):
    if require_request_equivalence and request_equivalence is None:
        raise ExecutionFailure(
            "Root routing needs prepared request equivalence"
        )
    resources = ExternalResources(
        is_sandbox=prepared.context.workspace_is_sandbox,
        root=prepared.context.workspace_root,
    )
    # Тот же снимок, из которого `assemble_run_context` соберёт настройки ctx.
    loaded = settings_from_snapshot(
        prepared.context.settings, prepared.context.runtime
    )
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
            raise ExecutionFailure(f"No prepared tool factory: {name}")
        tools[name] = {
            **deepcopy(metadata),
            "tool_fabric": candidates[name]["tool_fabric"],
        }
    router = ModelRouter(
        loaded, level_provider=lambda: loaded.settings.level or "default"
    )
    operations.router = router
    try:
        ctx = assemble_run_context(
            prepared.context,
            RunContextBindings(
                os_backend=resources,
                skills_registry=resources,
                logger=logger,
                tools_data=tools,
                model_router=router,
                thread_store=store,
                agent_store=ExternalAgentStore(),
                operations=operations,
                execution_patch=workflow.patched,
                prepared_request_equivalence=request_equivalence,
                subagent_independent_auto=subagent_independent_auto,
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
                collect_garbage=lambda: None,
            ),
        )
    except ValueError as exc:
        if request_equivalence is None:
            raise
        raise ExecutionFailure("Invalid prepared routing context") from exc

    seed_prepared_subagent_definitions(ctx, prepared.subagent_definitions)
    operations.router_provider = lambda: ctx.model_router
    # Шаги хода защищены историей workflow: допустим persist: turn.
    ctx.durable_execution = True
    ctx.model_transport_configured = prepared.model_transport_configured
    ctx.provider_currency = prepared.provider_currency
    return ctx
