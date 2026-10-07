"""Real core imports and builtin preparation under normal workflow restrictions."""

from temporalio import workflow

from gc_core_temporalio import sandbox_passthrough  # noqa: F401

from gc_core.agent_executor import AgentExecutor
from gc_core.config_modules.root import AppConfig
from gc_core.tools.registry import get_builtin_tools_data

# Deliberately execute registration in the sandbox, not just import its symbol.
BUILTINS = get_builtin_tools_data(AppConfig())


@workflow.defn
class ExecutorImportWorkflow:
    @workflow.run
    async def run(self) -> list[str]:
        return [AgentExecutor.__name__, *sorted(BUILTINS)]
