"""Real core imports and builtin preparation under normal workflow restrictions."""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    import pydantic  # noqa: F401

from gc_core.agent_executor import AgentExecutor
from gc_core.tools.registry import get_builtin_tools_data

# Deliberately execute registration in the sandbox, not just import its symbol.
BUILTINS = get_builtin_tools_data({})


@workflow.defn
class ExecutorImportWorkflow:
    @workflow.run
    async def run(self) -> list[str]:
        return [AgentExecutor.__name__, *sorted(BUILTINS)]
