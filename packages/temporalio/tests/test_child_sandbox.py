from gc_core_temporalio.child_workflow import UniversalSubagentWorkflow
from temporalio import workflow
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner


async def test_real_subagent_workflow_prepares_in_standard_sandbox():
    SandboxedWorkflowRunner().prepare_workflow(
        workflow._Definition.must_from_class(UniversalSubagentWorkflow)
    )


def test_early_child_messages_are_drained_without_enqueue_callback():
    from types import SimpleNamespace

    child = UniversalSubagentWorkflow()
    message = {"role": "user", "content": "early signal"}
    child.pending_messages = [message]
    with child.input_scope(
        SimpleNamespace(), parent_call_id="parent"
    ) as scope:
        assert scope.drain() == [message]
    assert child.pending_messages == []


def test_early_child_messages_not_consumed_return_to_pending():
    from types import SimpleNamespace

    child = UniversalSubagentWorkflow()
    message = {"role": "user", "content": "early signal"}
    child.pending_messages = [message]
    with child.input_scope(SimpleNamespace(), parent_call_id="parent"):
        assert child.pending_messages == []
    assert child.pending_messages == [message]
