"""Worker resource actors using an injected durable effect ledger.

The ledger protocol is implemented by gc_ua_temporalio.storage.effects.EffectLedger.
Live futures provide singleflight only; receipts and uncertain outcomes live in
that ledger. Resource handles never migrate or reconnect after process loss.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Protocol

from gc_core.execution.contracts import (
    ArtifactRef,
    GuardPreconditionChanged,
    OutcomeUnknown,
    StateDelta,
    ToolEffectRequest,
    ToolResult,
)
from gc_core.execution.resource_contracts import (
    BackgroundProcessRef,
    ResourceEffectResult,
    ResourceOwnerLost,
    ResourceOwnerRef,
)
from gc_core.execution.resource_scope import capture_background_processes

_OWNER_OPERATION = "__resource_owner__"


class ResourceEffectLedger(Protocol):
    def get(self, owner, operation): ...
    def begin(self, owner, operation, workspace, digest, plan): ...
    def finish(self, owner, operation, status, result=None): ...
    def result(self, row): ...


def _fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True).encode()
    ).hexdigest()


def _restore_result(data: dict) -> ResourceEffectResult:
    value = dict(data["result"])
    value["delta"] = StateDelta(**value.get("delta", {}))
    value["artifacts"] = [
        ArtifactRef(**artifact) for artifact in value.get("artifacts", [])
    ]
    return ResourceEffectResult(
        result=ToolResult(**value),
        background_processes=[
            BackgroundProcessRef(
                owner=ResourceOwnerRef(**process["owner"]),
                operation_id=process["operation_id"],
                pid=process["pid"],
                backend=process["backend"],
            )
            for process in data.get("background_processes", [])
        ],
    )


async def _close_mcp_owner() -> None:
    from gc_core.tools.connect_mcp.persistent_stdio import (
        PersistentStdioRegistry,
    )
    from gc_core.tools.connect_mcp.stream_client import _SESSION_MANAGER

    try:
        await _SESSION_MANAGER.aclose_all(in_owner_task=True)
    finally:
        await PersistentStdioRegistry.instance().aclose_all()


@dataclass
class _PrepareContext:
    context_id: str
    callback: Any
    future: Any


class LiveResourceOwner:
    """One task owns discovery, calls and teardown of MCP context managers."""

    def __init__(self, ref, workspace, ledger, bootstrap, close):
        self.ref, self.workspace, self.ledger = ref, workspace, ledger
        self._queue = asyncio.Queue()
        self._pending: dict[str, tuple[str, asyncio.Future]] = {}
        self._contexts = {}
        self._context_futures = {}
        self._ready = asyncio.get_running_loop().create_future()
        self._accepting = True
        self._close = close
        self._task = asyncio.create_task(
            self._run(bootstrap), name=f"resources:{ref.owner_id}"
        )

    async def ready(self) -> ResourceOwnerRef:
        await asyncio.shield(self._ready)
        return self.ref

    def receipt(self, row) -> ResourceEffectResult:
        status = row["status"]
        if status == "SUCCEEDED":
            return _restore_result(self.ledger.result(row))
        if status == "REJECTED":
            raise GuardPreconditionChanged(
                "Resource effect precondition was rejected"
            )
        raise OutcomeUnknown(f"Resource effect has unresolved status {status}")

    async def execute(
        self, request: ToolEffectRequest, *, context_id: str | None = None
    ) -> ResourceEffectResult:
        if (
            not request.operation_id
            or request.operation_id == _OWNER_OPERATION
        ):
            raise ValueError(
                "Resource effects require a distinct operation_id"
            )
        digest = _fingerprint(
            asdict(request)
            if context_id is None
            else {"effect": asdict(request), "context_id": context_id}
        )
        # No await before registration: simultaneous activity retries share one future.
        existing = self._pending.get(request.operation_id)
        if existing is not None:
            if existing[0] != digest:
                raise OutcomeUnknown(
                    "Operation identity reused for a different effect"
                )
            return deepcopy(await asyncio.shield(existing[1]))
        row = self.ledger.get(self.ref.owner_id, request.operation_id)
        if row is not None:
            if row["digest"] != digest:
                raise OutcomeUnknown(
                    "Operation identity reused for a different effect"
                )
            return self.receipt(row)
        if not self._accepting or self._task.done():
            raise ResourceOwnerLost("Resource owner is closed or unavailable")
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(
            lambda done: None if done.cancelled() else done.exception()
        )
        self._pending[request.operation_id] = (digest, future)
        self._queue.put_nowait((context_id, deepcopy(request), digest, future))
        return deepcopy(await asyncio.shield(future))

    async def prepare_context(self, context_id, callback):
        if not context_id:
            raise ValueError("Child context_id is required")
        if not self._accepting or self._task.done():
            raise ResourceOwnerLost("Resource owner is closed or unavailable")
        future = self._context_futures.get(context_id)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            future.add_done_callback(
                lambda done: None if done.cancelled() else done.exception()
            )
            self._context_futures[context_id] = future
            self._queue.put_nowait(
                _PrepareContext(context_id, callback, future)
            )
        return await asyncio.shield(future)

    async def close(self) -> None:
        if self._accepting:
            self._accepting = False
            self._queue.put_nowait(None)
        # Cancelling the RPC waiter never cancels the owner task or its anyio scopes.
        await asyncio.shield(self._task)

    async def _invoke(self, ctx, request, digest, future):
        from gc_core.execution.tool_effects import execute_native_tool_effect

        owner, operation = self.ref.owner_id, request.operation_id
        began = False
        try:
            old = self.ledger.begin(
                owner,
                operation,
                self.workspace,
                digest,
                {
                    "resource_owner": asdict(self.ref),
                    "effect": asdict(request),
                },
            )
            if old is not None:
                future.set_result(self.receipt(old))
                return
            began = True
            processes = []

            def capture(pid, backend):
                processes.append(
                    BackgroundProcessRef(self.ref, operation, pid, backend)
                )

            with capture_background_processes(capture):
                result = await execute_native_tool_effect(request, ctx=ctx)
            receipt = ResourceEffectResult(result, processes)
            self.ledger.finish(owner, operation, "SUCCEEDED", asdict(receipt))
            future.set_result(receipt)
        except GuardPreconditionChanged as exc:
            if began:
                self.ledger.finish(owner, operation, "REJECTED")
            future.set_exception(exc)
        except BaseException as exc:
            if began:
                self.ledger.finish(owner, operation, "UNKNOWN")
            error = OutcomeUnknown(
                f"Resource effect has no committed receipt: {type(exc).__name__}"
            )
            error.__cause__ = exc
            future.set_exception(error)
            # A failed MCP exchange may have invalidated its live session.
            self._accepting = False
            raise
        finally:
            self._pending.pop(operation, None)

    async def _run(self, bootstrap):
        from gc_core.mcp_session_owner import reset_owner, set_owner, use_owner
        from gc_core.tools.connect_mcp.durable_scope import durable_mcp_calls

        failed = False
        owner_key = f"native:{self.ref.process_epoch}:{self.ref.owner_id}"
        with use_owner(owner_key), durable_mcp_calls():
            bootstrap_owner_reset = None
            try:
                ctx = await bootstrap()
                # Native RunContext preparation assigns its own registry owner;
                # retain exactly that owner for calls and teardown after discovery.
                bootstrap_owner_reset = set_owner(
                    getattr(ctx, "mcp_session_owner", None) or owner_key
                )
                self.ledger.finish(
                    self.ref.owner_id,
                    _OWNER_OPERATION,
                    "READY",
                    asdict(self.ref),
                )
                self._ready.set_result(None)
                await self._serve(ctx)
            except BaseException as exc:
                failed = True
                if not self._ready.done():
                    self._ready.set_exception(exc)
            finally:
                self._accepting = False
                # Teardown runs inside the task that entered all MCP SDK scopes.
                try:
                    await self._close()
                finally:
                    self.ledger.finish(
                        self.ref.owner_id,
                        _OWNER_OPERATION,
                        "OWNER_LOST" if failed else "CLOSED",
                        asdict(self.ref),
                    )
                    for _, future in self._pending.values():
                        if not future.done():
                            future.set_exception(
                                ResourceOwnerLost(
                                    "Resource owner stopped before execution"
                                )
                            )
                    self._pending.clear()
                    for future in self._context_futures.values():
                        if not future.done():
                            future.set_exception(
                                ResourceOwnerLost(
                                    "Owner stopped before child preparation"
                                )
                            )
                    self._contexts.clear()
                    self._context_futures.clear()
                    if bootstrap_owner_reset is not None:
                        reset_owner(bootstrap_owner_reset)

    async def _serve(self, ctx):
        while True:
            item = await self._queue.get()
            if item is None:
                break
            if isinstance(item, _PrepareContext):
                try:
                    child = await item.callback(ctx)
                    self._contexts[item.context_id] = child
                    item.future.set_result(child)
                except BaseException as exc:
                    item.future.set_exception(exc)
                    raise
            else:
                context_id, *effect = item
                effect_ctx = (
                    ctx
                    if context_id is None
                    else self._contexts.get(context_id)
                )
                if effect_ctx is None:
                    effect[-1].set_exception(
                        ResourceOwnerLost(
                            "Child context was not prepared on owner"
                        )
                    )
                    self._pending.pop(effect[0].operation_id, None)
                    continue
                await self._invoke(effect_ctx, *effect)


class WorkerResourceHost:
    """Route explicit owner identities; never recreate an old logical session."""

    def __init__(self, worker_id: str, *, ledger: ResourceEffectLedger):
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        self.worker_id = worker_id
        self.process_epoch = uuid.uuid4().hex
        self.ledger = ledger
        self._owners: dict[str, LiveResourceOwner] = {}

    async def open(
        self,
        owner_id: str,
        workspace: str,
        bootstrap: Callable[[], Awaitable[Any]],
        *,
        close: Callable[[], Awaitable[None]] = _close_mcp_owner,
    ) -> ResourceOwnerRef:
        if not owner_id or not workspace:
            raise ValueError("owner_id and workspace must not be empty")
        owner = self._owners.get(owner_id)
        if owner is None:
            if self.ledger.get(owner_id, _OWNER_OPERATION) is not None:
                raise ResourceOwnerLost(
                    "The original resource owner cannot be recreated"
                )
            ref = ResourceOwnerRef(
                self.worker_id, self.process_epoch, owner_id
            )
            self.ledger.begin(
                owner_id,
                _OWNER_OPERATION,
                f"resource-owner:{owner_id}",
                _fingerprint(asdict(ref)),
                {"resource_owner": asdict(ref)},
            )
            owner = LiveResourceOwner(
                ref, workspace, self.ledger, bootstrap, close
            )
            self._owners[owner_id] = owner
        elif owner.workspace != workspace:
            raise ValueError(
                "Owner identity reused with a different workspace"
            )
        elif not owner._accepting or owner._task.done():
            raise ResourceOwnerLost(
                "Closed owner cannot be reopened under its old identity"
            )
        try:
            return await owner.ready()
        except Exception:
            # A failed bootstrap cannot be retried as a new live owner, but
            # its completed actor and captured contexts need not stay resident.
            await owner.close()
            self.forget(owner.ref)
            raise

    def _resolve(self, ref: ResourceOwnerRef) -> LiveResourceOwner:
        if (
            ref.schema_version != 1
            or ref.worker_id != self.worker_id
            or ref.process_epoch != self.process_epoch
        ):
            raise ResourceOwnerLost(
                "Resource owner belongs to another worker process"
            )
        owner = self._owners.get(ref.owner_id)
        if owner is None:
            raise ResourceOwnerLost(
                "Resource owner is not present on this host"
            )
        return owner

    async def execute(
        self,
        ref: ResourceOwnerRef,
        request: ToolEffectRequest,
        *,
        context_id: str | None = None,
    ) -> ResourceEffectResult:
        return await self._resolve(ref).execute(request, context_id=context_id)

    async def prepare_context(self, ref, context_id, callback):
        """Prepare a child using the task that owns its live MCP resources.

        callback and its result are host-local dependencies, never serialized.
        The existing owner fence governs restart and cancellation semantics.
        """
        return await self._resolve(ref).prepare_context(context_id, callback)

    def operation(self, ref: ResourceOwnerRef, operation_id: str) -> dict:
        """Read durable status after activity cancellation or resource host restart."""
        row = self.ledger.get(ref.owner_id, operation_id)
        if row is None:
            return {"status": "NOT_STARTED"}
        plan = (
            json.loads(row["plan"])
            if isinstance(row["plan"], str)
            else row["plan"]
        )
        if plan.get("resource_owner") != asdict(ref):
            raise ResourceOwnerLost(
                "Operation belongs to a different resource owner"
            )
        if row["status"] == "STARTED":
            owner = self._owners.get(ref.owner_id)
            alive = (
                ref.process_epoch == self.process_epoch
                and owner is not None
                and not owner._task.done()
            )
            if not alive:
                self.ledger.finish(ref.owner_id, operation_id, "UNKNOWN")
                row = self.ledger.get(ref.owner_id, operation_id)
        return {"status": row["status"], "result": self.ledger.result(row)}

    async def close(self, ref: ResourceOwnerRef) -> None:
        await self._resolve(ref).close()

    def forget(self, ref: ResourceOwnerRef) -> None:
        """Drop a completed actor; its durable owner tombstone is retained."""
        if (
            ref.schema_version != 1
            or ref.worker_id != self.worker_id
            or ref.process_epoch != self.process_epoch
        ):
            raise ResourceOwnerLost(
                "Cannot forget another worker's resource owner"
            )
        owner = self._owners.get(ref.owner_id)
        if owner is None:
            return
        row = self.ledger.get(ref.owner_id, _OWNER_OPERATION)
        if (
            not owner._task.done()
            or not row
            or row["status"] not in {"CLOSED", "OWNER_LOST"}
        ):
            raise RuntimeError(
                "Resource owner must finish before it can be forgotten"
            )
        self._owners.pop(ref.owner_id)
