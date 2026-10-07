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
    "AgentClient": "gc_core_temporalio.client",
    "TemporalDurability": "gc_core_temporalio.client",
    "session_workflow_id": "gc_core_temporalio.client",
    "AgentWorkflow": "gc_core_temporalio.workflow",
    "SubagentWorkflow": "gc_core_temporalio.child_workflow",
    "RunRequest": "gc_core_temporalio.contracts",
    "UserMessage": "gc_core_temporalio.contracts",
    "WorkerConfig": "gc_core_temporalio.provider",
    "AgentProfile": "gc_core_temporalio.provider",
    "run_worker": "gc_core_temporalio.worker",
    "default_build_id": "gc_core_temporalio.worker",
}

__all__ = [
    "AgentClient",
    "AgentPlugin",
    "AgentProfile",
    "AgentSession",
    "AgentWorkflow",
    "RunRequest",
    "SubagentWorkflow",
    "TemporalDurability",
    "UserMessage",
    "WorkerConfig",
    "default_build_id",
    "run_worker",
    "session_workflow_id",
]


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(name)
    return getattr(import_module(module), name)
