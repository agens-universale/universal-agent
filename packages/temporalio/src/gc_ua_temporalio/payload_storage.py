"""Крупные payload истории Temporal — во внешнем хранилище (external storage SDK).

Вход ``agent.model.request`` несёт всю переписку, и без этого история workflow
растёт квадратично. ``DataConverter`` кладёт payload больше порога в хранилище и
пишет в историю короткую ссылку; core и код workflow об этом не знают.

Хранилище должно быть доступно всем, кто кодирует или читает payload: worker'у,
серверу API и клиенту ``gc-temporal-native``. Temporal blob'ы не удаляет —
очистка по возрасту (``prune_file_payloads`` или lifecycle-правило S3) обязательна.
Повторная запись и чтение blob'а обновляют его mtime: очистка по возрасту не
трогает blob'ы, на которые ещё ссылаются живые сессии.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field
from temporalio.api.common.v1 import Payload
from temporalio.converter import (
    DataConverter,
    ExternalStorage,
    StorageDriver,
    StorageDriverClaim,
    StorageDriverRetrieveContext,
    StorageDriverStoreContext,
)

DEFAULT_THRESHOLD = 32 * 1024


class PayloadStorageConfig(BaseModel):
    """Настройка ``payload_storage`` в ``NativeWorkerConfig``."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["file", "s3"] = "file"
    #: Каталог для ``file``.
    path: str | None = None
    #: Bucket и префикс для ``s3``.
    bucket: str | None = None
    #: Payload меньше порога остаются прямо в истории (их видно в Temporal UI).
    threshold_bytes: int = Field(default=DEFAULT_THRESHOLD, ge=0)


class FileStorageDriver(StorageDriver):
    """Payload в каталоге; ключ — sha256 сериализованных байт, запись атомарна."""

    def __init__(self, root: str | os.PathLike, *, driver_name="ua.file"):
        self.root = Path(root).absolute()
        self._name = driver_name

    def name(self) -> str:
        return self._name

    def _path(self, key: str) -> Path:
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("Invalid payload storage key")
        return self.root / key[:2] / f"{key}.bin"

    def _write(self, data: bytes) -> str:
        key = hashlib.sha256(data).hexdigest()
        path = self._path(key)
        try:
            intact = hashlib.sha256(path.read_bytes()).hexdigest() == key
        except FileNotFoundError:
            intact = False
        if intact:
            # Тот же blob нужен новой ссылке: продлить его жизнь для очистки.
            os.utime(path)
            return key
        # Нет или битый (сбой питания до fsync) — записать заново.
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return key

    def _read(self, key: str) -> bytes:
        path = self._path(key)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != key:
            raise ValueError("Stored payload digest mismatch")
        try:
            os.utime(path)
        except OSError:  # хранилище только на чтение у клиента — не помеха
            pass
        return data

    async def store(
        self,
        context: StorageDriverStoreContext,
        payloads: Sequence[Payload],
    ) -> list[StorageDriverClaim]:
        keys = await asyncio.to_thread(
            lambda: [self._write(p.SerializeToString()) for p in payloads]
        )
        return [StorageDriverClaim({"sha256": key}) for key in keys]

    async def retrieve(
        self,
        context: StorageDriverRetrieveContext,
        claims: Sequence[StorageDriverClaim],
    ) -> list[Payload]:
        blobs = await asyncio.to_thread(
            lambda: [self._read(c.claim_data["sha256"]) for c in claims]
        )
        return [Payload.FromString(blob) for blob in blobs]


def prune_file_payloads(root: str | os.PathLike, *, max_age_seconds: float):
    """Удалить blob'ы старше срока; срок должен быть больше retention namespace."""
    deadline = time.time() - max_age_seconds
    removed = 0
    for path in Path(root).glob("*/*.bin"):
        if path.stat().st_mtime < deadline:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def _s3_driver(config: PayloadStorageConfig) -> StorageDriver:
    try:
        import aioboto3
        from temporalio.contrib.aws.s3driver import S3StorageDriver
        from temporalio.contrib.aws.s3driver.aioboto3 import (
            new_aioboto3_client,
        )
    except ImportError as exc:  # pragma: no cover — зависит от окружения
        raise RuntimeError(
            "payload_storage.kind='s3' requires the 'aioboto3' package"
        ) from exc
    session = aioboto3.Session()
    return S3StorageDriver(
        new_aioboto3_client(session.client("s3")), bucket=config.bucket
    )


def external_storage(config: PayloadStorageConfig | None):
    if config is None:
        return None
    if config.kind == "file":
        if not config.path:
            raise ValueError("payload_storage.path is required for kind=file")
        driver = FileStorageDriver(config.path)
    else:
        if not config.bucket:
            raise ValueError("payload_storage.bucket is required for kind=s3")
        driver = _s3_driver(config)
    return ExternalStorage(
        drivers=[driver], payload_size_threshold=config.threshold_bytes
    )


def data_converter(config: PayloadStorageConfig | None) -> DataConverter:
    """Конвертер для клиента и worker'а; без настройки — стандартный."""
    converter = DataConverter.default
    storage = external_storage(config)
    if storage is None:
        return converter
    return dataclasses.replace(converter, external_storage=storage)


__all__ = [
    "DEFAULT_THRESHOLD",
    "FileStorageDriver",
    "PayloadStorageConfig",
    "data_converter",
    "external_storage",
    "prune_file_payloads",
]
