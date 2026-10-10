"""Агент ядра (``gc_core``) в Temporal: durable workflow и activities.

Воркер — нативные ``temporalio`` ``Client``/``Worker`` с ``AgentPlugin``;
приложение — тот же ``AsyncAgent`` с ``durable=TemporalDurability(...)`` или
``AgentSession`` (тред: отправить и дождаться ответа)::

    from gc_core_temporalio import AgentPlugin, TemporalDurability

Экспорты ленивые: модули workflow импортируются в песочнице Temporal, и
импорт пакета не должен тянуть за собой воркер и плагин.
"""

from importlib import import_module

_CLIENT = "gc_core_temporalio.client"
_STEPS = "gc_core_temporalio.steps"
_WORKER = "gc_core_temporalio.worker"

_EXPORTS = {
    "AgentPlugin": "gc_core_temporalio.plugin",
    "AgentSession": _CLIENT,
    "AnswerTruncated": _CLIENT,
    "AgentClient": _CLIENT,
    "TemporalDurability": _CLIENT,
    "MessageNotProcessed": _CLIENT,
    "SessionFailed": _CLIENT,
    "SessionStuck": _CLIENT,
    "SettingsMismatch": _CLIENT,
    "TurnFailed": _CLIENT,
    "session_workflow_id": _CLIENT,
    "UniversalAgentWorkflow": "gc_core_temporalio.workflow",
    "UniversalSubagentWorkflow": "gc_core_temporalio.child_workflow",
    "RunRequest": "gc_core_temporalio.contracts",
    "UserMessage": "gc_core_temporalio.contracts",
    "WorkerConfig": "gc_core_temporalio.provider",
    "AgentProfile": "gc_core_temporalio.provider",
    "run_worker": _WORKER,
    "agent_step": _STEPS,
    "AgentStepResult": _STEPS,
    "step_workflow_id": _STEPS,
    "default_build_id": _WORKER,
}

__all__ = [
    "AgentClient",
    "AgentPlugin",
    "AgentProfile",
    "AgentSession",
    "AgentStepResult",
    "AnswerTruncated",
    "MessageNotProcessed",
    "RunRequest",
    "SessionFailed",
    "SessionStuck",
    "SettingsMismatch",
    "TemporalDurability",
    "TurnFailed",
    "UniversalAgentWorkflow",
    "UniversalSubagentWorkflow",
    "UserMessage",
    "WorkerConfig",
    "agent_step",
    "default_build_id",
    "run_worker",
    "session_workflow_id",
    "step_workflow_id",
]


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(name)
    return getattr(import_module(module), name)
