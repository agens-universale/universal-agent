"""Крупные payload уходят во внешнее хранилище; в истории остаётся ссылка."""

import os
import shutil
import time
from datetime import timedelta

import pytest
from gc_ua_temporalio.payload_storage import (
    FileStorageDriver,
    PayloadStorageConfig,
    data_converter,
    prune_file_payloads,
)
from temporalio import activity, workflow
from temporalio.api.common.v1 import Payload
from temporalio.converter import StorageDriverClaim


async def test_file_driver_roundtrip_and_digest_check(tmp_path):
    driver = FileStorageDriver(tmp_path)
    payload = Payload(data=b"x" * 100, metadata={"encoding": b"binary/plain"})
    [claim] = await driver.store(None, [payload])
    [restored] = await driver.retrieve(None, [claim])
    assert restored == payload
    path = next(tmp_path.glob("*/*.bin"))
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError):
        await driver.retrieve(None, [claim])


async def test_file_driver_rewrites_corrupt_blob_and_refreshes_mtime(tmp_path):
    driver = FileStorageDriver(tmp_path)
    payload = Payload(data=b"y" * 100, metadata={"encoding": b"binary/plain"})
    [claim] = await driver.store(None, [payload])
    path = next(tmp_path.glob("*/*.bin"))
    stale = time.time() - 3600
    os.utime(path, (stale, stale))
    # Повторная ссылка на тот же blob продлевает его жизнь для очистки.
    await driver.store(None, [payload])
    assert path.stat().st_mtime > stale
    assert prune_file_payloads(tmp_path, max_age_seconds=60) == 0
    # Битый blob (сбой питания) перезаписывается следующей записью.
    path.write_bytes(b"")
    await driver.store(None, [payload])
    [restored] = await driver.retrieve(None, [claim])
    assert restored == payload
    assert not list(tmp_path.glob("*/*.tmp"))


def test_worker_config_stores_payloads_by_default(tmp_path):
    from gc_ua_temporalio.native_provider import NativeWorkerConfig

    base = {
        "receipts_database": str(tmp_path / "receipts.sqlite"),
        "profiles": {},
    }
    config = NativeWorkerConfig.model_validate(base)
    assert config.payload_storage == PayloadStorageConfig(
        kind="file", path=str(tmp_path / "payloads")
    )
    disabled = NativeWorkerConfig.model_validate(
        {**base, "payload_storage": None}
    )
    assert disabled.payload_storage is None


async def test_file_driver_rejects_path_outside_root(tmp_path):
    driver = FileStorageDriver(tmp_path)
    with pytest.raises(ValueError):
        await driver.retrieve(
            None, [StorageDriverClaim({"sha256": "../../etc/passwd"})]
        )


def test_prune_removes_only_old_payloads(tmp_path):
    old = tmp_path / "ab" / ("a" * 64 + ".bin")
    fresh = tmp_path / "cd" / ("c" * 64 + ".bin")
    for path in (old, fresh):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"data")
    stale = time.time() - 3600
    os.utime(old, (stale, stale))
    assert prune_file_payloads(tmp_path, max_age_seconds=60) == 1
    assert not old.exists() and fresh.exists()


@activity.defn(name="echo_size")
async def echo_size(text: str) -> int:
    return len(text)


@workflow.defn(name="PayloadStorageProbe")
class PayloadStorageProbe:
    @workflow.run
    async def run(self, text: str) -> int:
        return await workflow.execute_activity(
            echo_size, text, start_to_close_timeout=timedelta(seconds=10)
        )


async def test_large_activity_input_is_stored_by_reference(tmp_path):
    from temporalio.client import Client
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("Temporal CLI is required")
    storage = PayloadStorageConfig(path=str(tmp_path / "blobs"))
    big = "y" * 200_000
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary
    ) as env:
        client = Client(
            env.client.service_client,
            namespace=env.client.namespace,
            data_converter=data_converter(storage),
        )
        async with Worker(
            client,
            task_queue="payloads",
            workflows=[PayloadStorageProbe],
            activities=[echo_size],
        ):
            handle = await client.start_workflow(
                PayloadStorageProbe.run,
                big,
                id="payloads",
                task_queue="payloads",
            )
            assert await handle.result() == len(big)
            history = await handle.fetch_history()
    assert list((tmp_path / "blobs").glob("*/*.bin"))
    assert history.to_json().count("y" * 1000) == 0
