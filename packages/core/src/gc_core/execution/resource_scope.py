"""Worker-only capture of live resources; handles never enter workflow DTOs."""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable

_BACKGROUND_CAPTURE: ContextVar[Callable[[int, str], None] | None] = (
    ContextVar(
        "worker_background_resource_capture",
        default=None,
    )
)


def resource_capture_active() -> bool:
    return _BACKGROUND_CAPTURE.get() is not None


def record_background_process(pid: int, backend: str) -> None:
    capture = _BACKGROUND_CAPTURE.get()
    if capture is not None:
        capture(pid, backend)


@contextmanager
def capture_background_processes(capture: Callable[[int, str], None]):
    token = _BACKGROUND_CAPTURE.set(capture)
    try:
        yield
    finally:
        _BACKGROUND_CAPTURE.reset(token)
