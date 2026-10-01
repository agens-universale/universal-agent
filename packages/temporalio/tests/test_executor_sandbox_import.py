from executor_sandbox_fixture import ExecutorImportWorkflow
from temporalio import workflow
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner


async def test_real_executor_and_builtin_registry_prepare_in_standard_sandbox():
    # No core passthrough and no relaxed restriction set: filesystem/network
    # access during import or registry construction must fail preparation.
    SandboxedWorkflowRunner().prepare_workflow(
        workflow._Definition.must_from_class(ExecutorImportWorkflow)
    )
