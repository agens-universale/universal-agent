"""``port``: локально — прежний getattr, в durable-исполнении — громкий отказ."""

from types import SimpleNamespace

import pytest
from gc_core.execution.contracts import ExecutionFailure
from gc_core.execution.ports import port


def test_local_context_without_port_keeps_local_fallback():
    assert port(SimpleNamespace(), "prompt_loader") is None


def test_durable_context_without_port_fails_loudly():
    ctx = SimpleNamespace(durable_execution=True)
    with pytest.raises(ExecutionFailure, match="prompt_loader"):
        port(ctx, "prompt_loader")


def test_bound_port_is_returned_in_both_modes():
    loader = object()
    assert (
        port(SimpleNamespace(prompt_loader=loader), "prompt_loader") is loader
    )
    durable = SimpleNamespace(durable_execution=True, prompt_loader=loader)
    assert port(durable, "prompt_loader") is loader


def test_sandbox_violation_is_recognised_by_class_name():
    from gc_core.execution.ports import is_sandbox_violation

    class NondeterminismError(Exception):
        pass

    class RestrictedWorkflowAccessError(NondeterminismError):
        pass

    assert is_sandbox_violation(RestrictedWorkflowAccessError("open"))
    assert is_sandbox_violation(NondeterminismError("x"))
    assert not is_sandbox_violation(ValueError("x"))
