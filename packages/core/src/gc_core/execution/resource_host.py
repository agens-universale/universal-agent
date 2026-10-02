"""Worker resource actors: one task owns MCP sessions and background processes.

Повторная доставка той же операции в этом процессе ждёт тот же результат.
Ресурсы не переживают процесс: после перезапуска worker'а владелец потерян, и
модель получает это как ошибку инструмента; следующий ход откроет нового.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable

from gc_core.execution.contracts import ToolEffectRequest
from gc_core.execution.resource_contracts import (
    BackgroundProcessRef,
    ResourceEffectResult,
    ResourceOwnerLost,
    ResourceOwnerRef,
)
from gc_core.execution.resource_scope import capture_background_processes


def _fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True).encode()
    ).hexdigest()


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

    def __init__(self, ref, workspace, bootstrap, close):
        self.ref, self.workspace = ref, workspace
        self._queue = asyncio.Queue()
        self._pending: dict[str, tuple[str, asyncio.Future]] = {}
        self._done: dict[str, tuple[str, ResourceEffectResult]] = {}
        self._contexts = {}
        self._context_futures = {}
        self._ready = asyncio.get_running_loop().create_future()
        self._accepting = True
        self.lost = False
        self._close = close
        self._task = asyncio.create_task(
            self._run(bootstrap), name=f"resources:{ref.owner_id}"
        )

    async def ready(self) -> ResourceOwnerRef:
        await asyncio.shield(self._ready)
        return self.ref

    async def execute(
        self, request: ToolEffectRequest, *, context_id: str | None = None
    ) -> ResourceEffectResult:
        if not request.operation_id:
            raise ValueError(
                "Resource effects require a distinct operation_id"
            )
        digest = _fingerprint(
            asdict(request)
            if context_id is None
            else {"effect": asdict(request), "context_id": context_id}
        )
        # No await before registration: simultaneous activity retries share one future.
        for known in (
            self._pending.get(request.operation_id),
            self._done.get(request.operation_id),
        ):
            if known is not None and known[0] != digest:
                raise ValueError(
                    "Operation identity reused for a different effect"
                )
        existing = self._pending.get(request.operation_id)
        if existing is not None:
            return deepcopy(await asyncio.shield(existing[1]))
        if request.operation_id in self._done:
            return deepcopy(self._done[request.operation_id][1])
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
        from gc_core.tools.connect_mcp.durable_scope import McpSessionLostError

        operation = request.operation_id
        try:
            processes = []

            def capture(pid, backend):
                processes.append(
                    BackgroundProcessRef(self.ref, operation, pid, backend)
                )

            with capture_background_processes(capture):
                result = await execute_native_tool_effect(request, ctx=ctx)
            receipt = ResourceEffectResult(result, processes)
            self._done[operation] = (digest, receipt)
            future.set_result(receipt)
        except McpSessionLostError as exc:
            # Сессия MCP умерла: дальнейшие вызовы этого владельца бессмысленны.
            future.set_exception(exc)
            self._accepting = False
        except asyncio.CancelledError:
            future.cancel()
            raise
        except Exception as exc:
            # Ошибка вызова — ошибка инструмента; сессия остаётся живой.
            future.set_exception(exc)
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
                    self.lost = failed
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
                except asyncio.CancelledError:
                    item.future.cancel()
                    raise
                except Exception as exc:
                    # Ошибка подготовки ребёнка не убивает ресурсы родителя.
                    item.future.set_exception(exc)
                    self._context_futures.pop(item.context_id, None)
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

    def __init__(self, worker_id: str):
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        self.worker_id = worker_id
        self.process_epoch = uuid.uuid4().hex
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
            ref = ResourceOwnerRef(
                self.worker_id, self.process_epoch, owner_id
            )
            owner = LiveResourceOwner(ref, workspace, bootstrap, close)
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

    async def close(self, ref: ResourceOwnerRef) -> None:
        await self._resolve(ref).close()

    def forget(self, ref: ResourceOwnerRef) -> None:
        """Drop a completed actor and its in-memory results."""
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
        if not owner._task.done():
            raise RuntimeError(
                "Resource owner must finish before it can be forgotten"
            )
        self._owners.pop(ref.owner_id)
