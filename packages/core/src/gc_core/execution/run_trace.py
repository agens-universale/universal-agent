"""Optional in-process observation of a root agent run."""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gc_core.execution.environment import utc_now

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelResponse:
    response_id: str
    model_name: str | None
    started_at: datetime
    messages: list[dict[str, Any]]
    usage: Any


@dataclass(frozen=True)
class CommittedMessages:
    messages: list[dict[str, Any]]
    timestamp: datetime


@dataclass(frozen=True)
class DiscardedResponse:
    response_id: str


TraceEvent = ModelResponse | CommittedMessages | DiscardedResponse
TraceObserver = Callable[[TraceEvent], None]
_observer = ContextVar[TraceObserver | None](
    "run_trace_observer", default=None
)
_root = ContextVar[object | None]("run_trace_root", default=None)


@contextmanager
def observe_run_trace(observer: TraceObserver) -> Iterator[None]:
    observer_token = _observer.set(observer)
    root_token = _root.set(None)
    try:
        yield
    finally:
        _root.reset(root_token)
        _observer.reset(observer_token)


@contextmanager
def bind_trace_root(state: object) -> Iterator[None]:
    if _observer.get() is None or _root.get() is not None:
        yield
        return
    token = _root.set(state)
    try:
        yield
    finally:
        _root.reset(token)


def trace_observer(state: object) -> TraceObserver | None:
    return _observer.get() if _root.get() is state else None


def _notify(observer: TraceObserver, event: TraceEvent) -> None:
    try:
        observer(event)
    except Exception:
        _log.exception("Run trace observer failed")


def trace_committed_messages(
    observer: TraceObserver | None, messages: list[dict[str, Any]]
) -> None:
    if observer is not None and messages:
        _notify(observer, CommittedMessages(messages, utc_now()))


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
        ),
    )


def start_model_trace(state: object) -> ModelRequestTrace | None:
    observer = trace_observer(state)
    return ModelRequestTrace(observer, utc_now()) if observer else None
