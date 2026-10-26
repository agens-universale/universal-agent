from gc_core_temporalio.child_workflow import UniversalSubagentWorkflow
from temporalio import workflow
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner


async def test_real_subagent_workflow_prepares_in_standard_sandbox():
    SandboxedWorkflowRunner().prepare_workflow(
        workflow._Definition.must_from_class(UniversalSubagentWorkflow)
    )
