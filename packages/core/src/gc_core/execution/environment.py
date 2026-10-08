"""Task-local sources of IDs and time, replaceable during durable execution."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

_id_source: ContextVar[Callable[[str], str] | None] = ContextVar(
    "execution_id", default=None
)
_utc_source: ContextVar[Callable[[], datetime]] = ContextVar(
    "execution_utc", default=lambda: datetime.now(UTC)
)


def supplied_id(prefix: str) -> str | None:
    source = _id_source.get()
    return source(prefix) if source else None


def utc_now() -> datetime:
    return _utc_source.get()()


@contextmanager
def execution_environment(
    *, new_id: Callable[[str], str], now: Callable[[], datetime]
) -> Iterator[None]:
    id_token = _id_source.set(new_id)
    utc_token = _utc_source.set(now)
    try:
        yield
    finally:
        for source, token in (
            (_utc_source, utc_token),
            (_id_source, id_token),
        ):
            try:
                source.reset(token)
            except ValueError:
                # Корутину закрыли в другом контексте (Temporal выгрузил run
                # из кэша, GeneratorExit при сборке мусора): в том контексте
                # восстанавливать нечего.
                pass
