"""Runtime-independent operation port. Core never imports Temporal."""

from typing import Protocol

from gc_core.execution.contracts import ModelRequest, ModelResult


class AgentOperations(Protocol):
    def next_operation_id(self, kind: str) -> str: ...

    def monotonic(self) -> float: ...

    async def model(self, request: ModelRequest) -> ModelResult: ...
