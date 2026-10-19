"""``S3KV`` — примитивы KV-движка на S3 и S3-совместимых (aiobotocore).

Версия — ETag объекта; условная запись — ``PutObject`` с ``If-None-Match:
*`` («только если нет») или ``If-Match: <ETag>``; отказ условия —
``PreconditionFailed`` (412) или ``ConditionalRequestConflict`` (409).
Список — ``ListObjectsV2`` (порядок байтов UTF-8). Учётные данные —
стандартная цепочка botocore (``AWS_ACCESS_KEY_ID``…, профиль, роль).

Без условной записи гарантий тредов нет: при первом обращении хранилище
пробует её и, если S3 не отказал там, где должен, отказывается работать
(``ConditionalWritesUnsupported``).
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Any

from gc_stores.kv.base import KV, Versioned
from gc_stores.sql.database import LoopBound

_REFUSED = frozenset({"PreconditionFailed", "ConditionalRequestConflict"})
_MISSING = frozenset({"NoSuchKey", "404"})
_PROBE = "_gc/probe/"


class ConditionalWritesUnsupported(RuntimeError):
    """S3 не поддерживает условную запись (If-Match/If-None-Match)."""


def driver() -> Any:
    try:
        from aiobotocore.session import get_session
    except ImportError as exc:
        raise ImportError(
            "Хранилищам S3 нужен драйвер aiobotocore: установите gc-stores "
            "с extra — gc-stores[s3] (docs/sdk/stores.md, раздел S3)"
        ) from exc
    return get_session


def _code(exc: Exception) -> str:
    response = getattr(exc, "response", None) or {}
    return str(response.get("Error", {}).get("Code", ""))


class S3KV(KV):
    """KV в бакете ``bucket`` под префиксом ``prefix``; ``endpoint_url`` —
    S3-совместимое (MinIO), ``client_options`` — в ``create_client``."""

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "",
        endpoint_url: str | None = None,
        region: str | None = None,
        client_options: dict[str, Any] | None = None,
    ) -> None:
        self.bucket = bucket
        self._prefix = prefix
        self._options = {
            "endpoint_url": endpoint_url,
            "region_name": region,
            **(client_options or {}),
        }
        self._client: Any = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._bound = LoopBound("S3-хранилище")
        self._checked = False

    async def _s3(self) -> Any:
        if self._bound.bind():
            self._client, self._stack = None, None
        if self._client is None:
            async with self._bound.lock:
                if self._client is None:
                    stack = contextlib.AsyncExitStack()
                    session = driver()()
                    self._client = await stack.enter_async_context(
                        session.create_client("s3", **self._options)
                    )
                    self._stack = stack
        return self._client

    def _key(self, key: str) -> str:
        return self._prefix + key

    async def get(self, key: str) -> Versioned | None:
        s3 = await self._s3()
        try:
            found = await s3.get_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:
            if _code(exc) in _MISSING:
                return None
            raise
        async with found["Body"] as body:
            data = await body.read()
        return Versioned(data, found["ETag"])

    async def put(
        self, key: str, data: bytes, *, if_version: str | None
    ) -> str | None:
        s3 = await self._s3()
        condition = (
            {"IfNoneMatch": "*"}
            if if_version is None
            else {"IfMatch": if_version}
        )
        try:
            written = await s3.put_object(
                Bucket=self.bucket, Key=self._key(key), Body=data, **condition
            )
        except Exception as exc:
            if _code(exc) in _REFUSED:
                return None
            raise
        return written["ETag"]

    async def put_any(self, key: str, data: bytes) -> str:
        s3 = await self._s3()
        written = await s3.put_object(
            Bucket=self.bucket, Key=self._key(key), Body=data
        )
        return written["ETag"]

    async def delete(self, key: str) -> None:
        s3 = await self._s3()
        await s3.delete_object(Bucket=self.bucket, Key=self._key(key))

    async def scan(
        self, prefix: str, *, start_after: str = "", limit: int = 1000
    ) -> list[str]:
        s3 = await self._s3()
        options: dict[str, Any] = {
            "Bucket": self.bucket,
            "Prefix": self._key(prefix),
            "MaxKeys": limit,
        }
        if start_after and start_after >= prefix:
            options["StartAfter"] = self._key(start_after)
        listed = await s3.list_objects_v2(**options)
        cut = len(self._prefix)
        return [item["Key"][cut:] for item in listed.get("Contents", [])]

    async def check(self) -> None:
        """Условная запись работает: повтор «только если нет» и чужой ETag
        отклоняются. Иначе — ``ConditionalWritesUnsupported``."""
        if self._checked:
            return
        probe = f"{_PROBE}{uuid.uuid4().hex}"
        try:
            first = await self.put(probe, b"1", if_version=None)
            again = await self.put(probe, b"2", if_version=None)
            foreign = await self.put(probe, b"3", if_version='"gc-stores"')
        finally:
            await self.delete(probe)
        if first is None or again is not None or foreign is not None:
            endpoint = self._options.get("endpoint_url") or "AWS S3"
            raise ConditionalWritesUnsupported(
                f"S3 ({endpoint}, бакет {self.bucket}) не поддерживает "
                "условную запись (If-Match/If-None-Match): треды на нём не "
                "гарантированы — нужен AWS S3, MinIO или другое S3 с "
                "условной записью"
            )
        self._checked = True

    async def close(self) -> None:
        """Закрыть клиент; повтор безвреден."""
        if self._stack is None:
            return
        if self._bound.bind():
            self._client, self._stack = None, None
            return
        stack, self._stack, self._client = self._stack, None, None
        await stack.aclose()
