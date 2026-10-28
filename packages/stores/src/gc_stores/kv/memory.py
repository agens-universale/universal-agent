"""``MemoryKV`` — KV в памяти процесса: проверка KV-движка без сервера.

``fail_on(операция, ключ)`` → ``True`` — перед этой операцией процесс
«падает» (``KvFault``): так проверяется, что прерванная запись не оставляет
видимых следов, а повтор с тем же ``op_id`` безопасен. Каждая операция
уступает loop — ``asyncio.gather`` двух писателей даёт чередование, как
сеть у настоящего хранилища.
"""

from __future__ import annotations

import asyncio
import bisect
from collections.abc import Callable

from gc_stores.kv.base import KV, Versioned


class KvFault(RuntimeError):
    """Внедрённый сбой: процесс «упал» перед операцией."""


class MemoryKV(KV):
    def __init__(self) -> None:
        self._data: dict[str, tuple[bytes, int]] = {}
        self._keys: list[str] = []
        self._counter = 0
        self.fail_on: Callable[[str, str], bool] | None = None

    async def _step(self, operation: str, key: str) -> None:
        await asyncio.sleep(0)
        if self.fail_on is not None and self.fail_on(operation, key):
            raise KvFault(f"{operation} {key}")

    def _store(self, key: str, data: bytes) -> str:
        self._counter += 1
        if key not in self._data:
            bisect.insort(self._keys, key)
        self._data[key] = (bytes(data), self._counter)
        return str(self._counter)

    async def get(self, key: str) -> Versioned | None:
        await self._step("get", key)
        found = self._data.get(key)
        return None if found is None else Versioned(found[0], str(found[1]))

    async def put(
        self, key: str, data: bytes, *, if_version: str | None
    ) -> str | None:
        await self._step("put", key)
        found = self._data.get(key)
        current = None if found is None else str(found[1])
        if current != if_version:
            return None
        return self._store(key, data)

    async def put_any(self, key: str, data: bytes) -> str:
        await self._step("put_any", key)
        return self._store(key, data)

    async def delete(self, key: str) -> None:
        await self._step("delete", key)
        if self._data.pop(key, None) is not None:
            self._keys.remove(key)

    conditional_delete = True

    async def delete_if(self, key: str, version: str) -> bool:
        await self._step("delete", key)
        found = self._data.get(key)
        if found is None or str(found[1]) != version:
            return False
        del self._data[key]
        self._keys.remove(key)
        return True

    async def scan(
        self, prefix: str, *, start_after: str = "", limit: int = 1000
    ) -> list[str]:
        await self._step("scan", prefix)
        if start_after and start_after >= prefix:
            start = bisect.bisect_right(self._keys, start_after)
        else:
            start = bisect.bisect_left(self._keys, prefix)
        found = []
        for key in self._keys[start:]:
            if not key.startswith(prefix) or len(found) >= limit:
                break
            found.append(key)
        return found

    def keys(self) -> list[str]:
        """Все ключи (для проверок: следы прерванной записи)."""
        return list(self._keys)
