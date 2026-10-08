"""Агент ядра (``gc_core``) в Temporal: durable workflow и activities.

Воркер — нативные ``temporalio`` ``Client``/``Worker`` с ``AgentPlugin``;
приложение — тот же ``AsyncAgent`` с ``durable=TemporalDurability(...)`` или
``AgentSession`` (тред: отправить и дождаться ответа)::

    from gc_core_temporalio import AgentPlugin, TemporalDurability

Экспорты ленивые: модули workflow импортируются в песочнице Temporal, и
импорт пакета не должен тянуть за собой воркер и плагин.
"""

from importlib import import_module

_EXPORTS = {
    "AgentPlugin": "gc_core_temporalio.plugin",
    "AgentSession": "gc_core_temporalio.client",
    "AnswerTruncated": "gc_core_temporalio.client",
    "AgentClient": "gc_core_temporalio.client",
    "TemporalDurability": "gc_core_temporalio.client",
    "MessageNotProcessed": "gc_core_temporalio.client",
    "SessionFailed": "gc_core_temporalio.client",
    "SessionStuck": "gc_core_temporalio.client",
    "SettingsMismatch": "gc_core_temporalio.client",
    "TurnFailed": "gc_core_temporalio.client",
    "session_workflow_id": "gc_core_temporalio.client",
    "UniversalAgentWorkflow": "gc_core_temporalio.workflow",
    "UniversalSubagentWorkflow": "gc_core_temporalio.child_workflow",
    "RunRequest": "gc_core_temporalio.contracts",
    "UserMessage": "gc_core_temporalio.contracts",
    "WorkerConfig": "gc_core_temporalio.provider",
    "AgentProfile": "gc_core_temporalio.provider",
    "run_worker": "gc_core_temporalio.worker",
    "agent_step": "gc_core_temporalio.steps",
    "AgentStepResult": "gc_core_temporalio.steps",
    "step_workflow_id": "gc_core_temporalio.steps",
    "default_build_id": "gc_core_temporalio.worker",
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
