"""Runtime-independent operation port. Core never imports Temporal."""

from typing import Protocol

from gc_core.execution.contracts import ModelRequest, ModelResult


class AgentOperations(Protocol):
    def next_operation_id(self, kind: str) -> str: ...

    def monotonic(self) -> float: ...

    async def model(self, request: ModelRequest) -> ModelResult: ...


def port(ctx, name):
    """Порт рантайма из контекста: ``getattr(ctx, name, None)``.

    Без порта ядро делает работу на месте (файл, сеть, процесс, часы). В
    durable-исполнении (``ctx.durable_execution``: код workflow Temporal) «на
    месте» нельзя: песочница уронит ход или функция тихо выключится. Поэтому
    там отсутствие порта — отказ хода с именем порта, а не тихий запасной
    путь. Локально поведение прежнее.
    """
    value = getattr(ctx, name, None)
    if value is None and getattr(ctx, "durable_execution", False):
        from gc_core.execution.contracts import ExecutionFailure

        raise ExecutionFailure(
            f"Runtime port {name!r} is not bound in durable execution: "
            "the local fallback would run inside workflow code"
        )
    return value


#: Нарушения песочницы Temporal (по имени: ядро не импортирует temporalio).
_SANDBOX_VIOLATIONS = frozenset(
    {"NondeterminismError", "RestrictedWorkflowAccessError"}
)


def is_sandbox_violation(exc: BaseException) -> bool:
    """Ошибка песочницы durable-рантайма (ввод-вывод в коде workflow).

    Её нельзя превращать в «ошибку инструмента» для модели: это баг
    интеграции, а не исход инструмента. Наружу — чтобы рантайм показал
    сбой задачи workflow, а не тихую деградацию.
    """
    return any(
        cls.__name__ in _SANDBOX_VIOLATIONS for cls in type(exc).__mro__
    )
