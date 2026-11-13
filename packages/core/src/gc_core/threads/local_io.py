"""Локальное файловое I/O: поток для async, прямой вызов для run_sync."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar

_INLINE = ContextVar("thread_store_inline_io", default=False)


@contextmanager
def synchronous_io():
    token = _INLINE.set(True)
    try:
        yield
    finally:
        _INLINE.reset(token)


async def local_io(function, *args):
    if _INLINE.get():
        return function(*args)
    return await asyncio.to_thread(function, *args)
