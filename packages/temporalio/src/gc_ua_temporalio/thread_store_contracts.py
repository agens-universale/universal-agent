"""Values passed to the configured native agent ThreadStore."""

from dataclasses import dataclass
from typing import Any

from gc_core.thread_store_durable import (
    ThreadStoreOperation,
    ThreadStoreReceipt,
    ThreadStoreSnapshot,
)


@dataclass
class ThreadStoreCall:
    store_id: str
    system_id: str
    thread_id: str
    operation: ThreadStoreOperation | None = None
    include_state: bool = True


@dataclass
class ThreadStoreReply:
    snapshot: ThreadStoreSnapshot | None = None
    receipt: ThreadStoreReceipt | None = None
    trace: list[dict[str, Any]] | None = None
