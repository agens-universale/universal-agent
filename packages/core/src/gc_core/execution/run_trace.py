"""Optional in-process observation of a trial's model calls and root steps."""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from gc_core.execution.environment import utc_now
from gc_core.usage.account import CATEGORY_UNCATEGORIZED
from gc_core.usage.context import current_usage_context

if TYPE_CHECKING:
    from gc_core.execution.state import State

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelResponse:
    response_id: str
    model_name: str | None
    started_at: datetime
    messages: list[dict[str, Any]]
    usage: Any
    backend_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class BackendResponse:
    payload: dict[str, Any]
    model_name: str | None
    started_at: datetime
    usage_category: str
    is_subagent: bool
    subagent_id: str | None


@dataclass(frozen=True)
class CommittedMessages:
    messages: list[dict[str, Any]]
    timestamp: datetime


@dataclass(frozen=True)
class DiscardedResponse:
    response_id: str


TraceEvent = (
    BackendResponse | ModelResponse | CommittedMessages | DiscardedResponse
)
TraceObserver = Callable[[TraceEvent], None]
_observer = ContextVar[TraceObserver | None](
    "run_trace_observer", default=None
)
_root = ContextVar[object | None]("run_trace_root", default=None)
_active_state = ContextVar[object | None]("run_trace_state", default=None)


@contextmanager
def observe_run_trace(observer: TraceObserver) -> Iterator[None]:
    observer_token = _observer.set(observer)
    root_token = _root.set(None)
    state_token = _active_state.set(None)
    try:
        yield
    finally:
        _active_state.reset(state_token)
        _root.reset(root_token)
        _observer.reset(observer_token)


@contextmanager
def bind_trace_root(state: object) -> Iterator[None]:
    if _observer.get() is None:
        yield
        return
    root = _root.get()
    root_token = _root.set(state if root is None else root)
    state_token = _active_state.set(state)
    try:
        yield
    finally:
        _active_state.reset(state_token)
        _root.reset(root_token)


def trace_observer(state: object) -> TraceObserver | None:
    return _observer.get() if _root.get() is state else None


def _notify(observer: TraceObserver, event: TraceEvent) -> None:
    try:
        observer(event)
    except Exception:
        _log.exception("Run trace observer failed")


def start_backend_trace() -> datetime | None:
    return utc_now() if _observer.get() is not None else None


def trace_backend_response(
    started_at: datetime | None,
    response: dict[str, Any],
    requested_model: str | None,
) -> None:
    observer = _observer.get()
    if observer is None or started_at is None:
        return
    usage_context = current_usage_context()
    state = _active_state.get()
    is_subagent = state is not None and state is not _root.get()
    model = response.get("model") or requested_model
    _notify(
        observer,
        BackendResponse(
            payload=response,
            model_name=str(model) if model else None,
            started_at=started_at,
            usage_category=usage_context.category
            if usage_context
            else CATEGORY_UNCATEGORIZED,
            is_subagent=is_subagent,
            subagent_id=getattr(state, "state_id", None)
            if is_subagent
            else None,
        ),
    )


def trace_committed_messages(
    observer: TraceObserver | None, messages: list[dict[str, Any]]
) -> None:
    if observer is not None and messages:
        _notify(observer, CommittedMessages(messages, utc_now()))


def trace_compacted_history(state: "State") -> None:
    """Publish user context installed by compaction's history replacement."""
    observer = trace_observer(state)
    if observer is not None:
        trace_committed_messages(
            observer,
            [
                message
                for message in state.messages
                if message.get("role") == "user"
            ],
        )


def trace_discarded_response(state: object, response_id: str) -> None:
    observer = trace_observer(state)
    if observer is not None:
        _notify(observer, DiscardedResponse(response_id))


@dataclass(frozen=True)
class ModelRequestTrace:
    observer: TraceObserver
    started_at: datetime


def finish_model_trace(
    trace: ModelRequestTrace | None,
    response: dict[str, Any],
    requested_model: str | None,
    messages: list[dict[str, Any]],
    response_id: str,
) -> None:
    if trace is None:
        return
    model = response.get("model") or requested_model
    _notify(
        trace.observer,
        ModelResponse(
            response_id,
            str(model) if model else None,
            trace.started_at,
            messages,
            response.get("usage"),
            backend_payload=response,
        ),
    )


def start_model_trace(state: object) -> ModelRequestTrace | None:
    observer = trace_observer(state)
    return ModelRequestTrace(observer, utc_now()) if observer else None
