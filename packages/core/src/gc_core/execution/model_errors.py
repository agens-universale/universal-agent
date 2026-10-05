"""Restore classified model failures without importing provider resources."""

from gc_core.execution.contracts import (
    ExecutionFailure,
    InfrastructureFailure,
    ModelFailure,
    ModelProviderError,
    OutcomeUnknown,
)

_DOMAIN_ERRORS = {cls.__name__: cls for cls in (ValueError, RuntimeError)}
_EXECUTION_ERRORS = {
    cls.__name__: cls
    for cls in (ExecutionFailure, InfrastructureFailure, OutcomeUnknown)
}


def restore_model_failure(failure: ModelFailure) -> Exception:
    """Preserve consumer policy without constructing HTTP clients/requests.

    Исключение помечено ``restored_model_failure``: это исход модельной
    операции, а не сбой кода вызывающего (durable-исполнитель отличает их).
    """
    exc = _restore_model_failure(failure)
    exc.restored_model_failure = True
    return exc


def _restore_model_failure(failure: ModelFailure) -> Exception:
    if failure.schema_version != 1:
        return ExecutionFailure("Unsupported ModelFailure schema_version")
    name, message = failure.error_type, failure.message
    if failure.kind == "execution":
        return _EXECUTION_ERRORS.get(name, ExecutionFailure)(message)
    if failure.kind == "provider":
        return ModelProviderError(failure)
    if failure.kind == "domain" and name in _DOMAIN_ERRORS:
        return _DOMAIN_ERRORS[name](message)
    return ExecutionFailure(f"Unsupported model failure type: {name}")
