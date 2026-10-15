"""Worker preparation and workflow assembly for the real SubagentRunner."""

from copy import copy
from dataclasses import replace

from gc_core.execution.contracts import ExecutionFailure
from gc_core.model_router import CircuitBreaker, ModelRouter
from gc_core.settings import LoadedSettings
from gc_core.states import State
from gc_core.tools.subagents.registry import (
    aprepare_subagent_definitions,
    resolve_subagent_definitions,
)
from gc_core.tools.subagents.runner import SubagentRunner
from gc_core.tools.subagents.runner_preparation import (
    resolve_prepared_subagent,
)
from temporalio import activity

from gc_core_temporalio.child_contracts import PreparedChild
from gc_core_temporalio.contracts import ActivityCall


async def prepare_child_context(call, parent_ctx):
    """Host-local callback; run inside the parent's actor when resources are live."""
    ref = call.child_context
    if not ref or ref["tool_name"] not in parent_ctx.tools_data:
        raise ExecutionFailure(
            "Child tool is absent from the real parent registry"
        )
    parent = copy(parent_ctx)
    parent.model_router = ModelRouter(
        parent,
        level_provider=lambda: ref["frozen_level"],
        force_legacy=ref["force_legacy"],
        breaker=CircuitBreaker.restore(ref["circuit_breaker"]),
    )
    runner = SubagentRunner(parent, None)
    runner._active_tool_name = ref["tool_name"]
    child = await runner._build_child_ctx(ref["tool_name"])
    child.run_id = call.owner_id
    child.mcp_session_owner = parent.mcp_session_owner
    child.mcp_status = parent.mcp_status
    child.registered_tool_names = set(child.tools_data)
    child._parent_context = parent
    return child


def seed_prepared_subagent_definitions(ctx, definitions):
    """Определения кастомных сабагентов workflow-контекста — готовые, с воркера:
    реестр не идёт в файлы (в песочнице workflow это запрещено)."""
    ctx.subagent_definitions = definitions


def prepared_subagent_factories(prepared):
    """Registry metadata with fixed builtin/file bindings, without discovery."""
    return {
        name: {"tool_fabric": None}
        for name in prepared.tool_metadata
        if resolve_prepared_subagent(name, prepared.subagent_definitions)
        is not None
    }


class ChildActivities:
    def __init__(self, activities):
        self.native = activities

    @activity.defn(name="universal_agent.subagent.prepare")
    async def prepare(self, call: ActivityCall) -> PreparedChild:
        return await self.native._recorded(call, PreparedChild, self._prepare)

    async def _prepare(self, call, child_ctx):
        parent_ctx = child_ctx._parent_context
        parent_state = State.from_execution_snapshot(call.data["parent_state"])
        parent_state.workspace = parent_ctx.workspace
        parent_state.logger = parent_ctx.logger
        runner = SubagentRunner(parent_ctx, parent_state)
        runner._active_tool_name = call.child_context["tool_name"]
        state = await runner._prepare_local_child_state()
        context = await self.native._prepare(call, child_ctx)
        await aprepare_subagent_definitions(
            parent_ctx, logger=parent_ctx.logger
        )
        context.subagent_definitions = resolve_subagent_definitions(parent_ctx)
        # Настройки ребёнка едут один раз — публичным снимком его контекста
        # (`context.context`); в подготовленном состоянии их нет. Workflow
        # подставляет типизированные настройки собранного из снимка контекста.
        return PreparedChild(context, replace(state, settings={}))


class PreparedChildContext:
    """Runtime adapter: preparation facts plus the ordinary child context."""

    def __init__(self, prepared, child_context):
        self.prepared, self.ctx = prepared, child_context

    def resolve_subagent(self, tool_name):
        return resolve_prepared_subagent(
            tool_name, self.prepared.context.subagent_definitions
        )

    def _child_settings(self):
        """Настройки ребёнка: собраны из публичного снимка его контекста."""
        return LoadedSettings(
            settings=self.ctx.settings, runtime=self.ctx.runtime
        )

    def build_child_config(self, _runner):
        return self._child_settings()

    async def build_child_context(self, runner, tool_name):
        self.ctx.model_router = runner._build_child_model_router(
            tool_name, self._child_settings()
        )
        self.ctx.operations.router = self.ctx.model_router
        self.ctx.operations.router_provider = lambda: self.ctx.model_router
        return self.ctx

    async def prepare_child_state(self, _runner):
        return replace(self.prepared.state, settings=self._child_settings())

    def release_child_context(self, _ctx):
        # The worker provider owns logs and the parent's live-resource lease.
        return None
