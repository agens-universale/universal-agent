"""Public exports loaded on demand; pure configuration/policy stays I/O-free."""

from importlib import import_module

_VALIDATION = "gc_core.intent_guard.validation_result"

_EXPORTS = {
    "build_finish_gate_feedback": (
        "gc_core.intent_guard.feedback",
        "build_finish_gate_feedback",
    ),
    "avalidate_final_report_vs_user_intent": (
        "gc_core.intent_guard.final_report_consistency",
        "avalidate_final_report_vs_user_intent",
    ),
    "FinishGateDecision": ("gc_core.intent_guard.guard", "FinishGateDecision"),
    "FinishGateGuard": ("gc_core.intent_guard.guard", "FinishGateGuard"),
    "FinishGateTraceDigest": (
        "gc_core.intent_guard.trace_digest",
        "FinishGateTraceDigest",
    ),
    "build_finish_gate_trace_digest": (
        "gc_core.intent_guard.trace_digest",
        "build_finish_gate_trace_digest",
    ),
    "ValidationResult": (
        _VALIDATION,
        "ValidationResult",
    ),
    "fail_open_validation_result": (
        _VALIDATION,
        "fail_open_validation_result",
    ),
    "parse_validator_response": (
        _VALIDATION,
        "parse_validator_response",
    ),
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module), attribute)
    globals()[name] = value
    return value
