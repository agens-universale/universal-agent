"""S3 durable-хранилище: тот же конформанс, что у файлового и SQLite, гонка за head
и живой прогон на MinIO (Docker; без него — пропуск)."""

import io
import itertools
import shutil
import socket
import subprocess
import threading
import time
import uuid

import pytest
from botocore.exceptions import ClientError
from gc_core.thread_store_durable import ThreadStoreOperation
from gc_testkit.durable_thread_store_conformance import (
    BEHAVIOUR_CHECKS,
    STRUCTURE_CHECKS,
    assert_durable_conforms,
)
from gc_ua_temporalio.storage.s3 import S3ThreadStore


def _error(code, status):
    return ClientError(
        {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "S3",
    )


class FakeS3:
    """Объекты в памяти с условной записью как у S3 (ETag, If-Match, If-None-Match)."""

    def __init__(self):
        self.objects = {}
        self.lock = threading.Lock()
        self.puts = []

    def get_object(self, Bucket, Key):
        with self.lock:
            if (Bucket, Key) not in self.objects:
                raise _error("NoSuchKey", 404)
            body, etag = self.objects[(Bucket, Key)]
        return {"Body": io.BytesIO(body), "ETag": etag}

    def put_object(
        self,
        Bucket,
        Key,
        Body,
        ContentType=None,
        IfMatch=None,
        IfNoneMatch=None,
    ):
        with self.lock:
            current = self.objects.get((Bucket, Key))
            if IfNoneMatch == "*" and current is not None:
                raise _error("PreconditionFailed", 412)
            if IfMatch is not None and (
                current is None or current[1] != IfMatch
            ):
                raise _error("PreconditionFailed", 412)
            etag = f'"{uuid.uuid4().hex}"'
            self.objects[(Bucket, Key)] = (bytes(Body), etag)
            self.puts.append(Key)
        return {"ETag": etag}

    def delete_object(self, Bucket, Key):
        with self.lock:
            self.objects.pop((Bucket, Key), None)

    def keys(self, bucket="b"):
        return sorted(key for b, key in self.objects if b == bucket)


def test_s3_store_conforms():
    counter = itertools.count()
    passed = assert_durable_conforms(
        lambda: S3ThreadStore("b", f"run-{next(counter)}", client=FakeS3())
    )
    assert len(passed) == len(STRUCTURE_CHECKS) + len(BEHAVIOUR_CHECKS)


def _claim(store, owner="owner-1"):
    snapshot = store.read_snapshot("sys", "t1", include_state=False)
    return store.commit_operation(
        "sys",
        "t1",
        ThreadStoreOperation(
            f"claim-{owner}",
            "claim_owner",
            {"owner_id": owner},
            generation=snapshot.generation,
            owner_id=owner,
        ),
    )


def _persist(
    store, op_id, messages, *, owner="owner-1", epoch=None, expected=None
):
    return store.commit_operation(
        "sys",
        "t1",
        ThreadStoreOperation(
            op_id,
            "persist",
            {"messages": messages, "meta": {"m": op_id}},
            expected_revision=expected,
            generation=0,
            owner_id=owner,
            owner_epoch=epoch,
        ),
    )


def test_head_race_reapplies_the_operation_and_leaves_no_orphans():
    """Чужая запись head между чтением и записью: операция применяется заново
    поверх новой версии, объекты проигравшей попытки удаляются."""
    client = FakeS3()
    store = S3ThreadStore("b", client=client)
    _claim(store)
    other = S3ThreadStore("b", client=client)
    original_put_head = store._put_head
    raced = {"done": False}

    def racing_put_head(base, head, etag):
        if not raced["done"]:
            raced["done"] = True
            _persist(
                other,
                "other",
                [{"role": "user", "content": "first", "id": "a"}],
            )
        return original_put_head(base, head, etag)

    store._put_head = racing_put_head
    receipt = _persist(
        store, "mine", [{"role": "user", "content": "second", "id": "b"}]
    )

    state = store.load_thread_state("sys", "t1")
    assert [m["id"] for m in state.messages] == ["a", "b"]
    assert state.max_seq == 2
    assert receipt.revision == store.read_snapshot("sys", "t1").revision
    head = client.objects[("b", "threads/sys/t1/head.json")][0]
    referenced = {key for key in client.keys() if key.endswith("head.json")}
    import json as _json

    doc = _json.loads(head)
    referenced |= set(doc["segments"]) | {
        r["key"] for r in doc["receipts"].values()
    }
    assert set(client.keys()) == referenced, (
        "объекты проигравшей попытки остались"
    )


def test_replayed_operation_writes_nothing():
    client = FakeS3()
    store = S3ThreadStore("b", client=client)
    _claim(store)
    first = _persist(
        store, "op", [{"role": "user", "content": "x", "id": "a"}]
    )
    puts = len(client.puts)
    again = _persist(
        store, "op", [{"role": "user", "content": "x", "id": "a"}]
    )
    assert again == first
    assert len(client.puts) == puts


def test_segments_are_compacted_and_old_ones_removed():
    from gc_ua_temporalio.storage import s3 as s3_module

    client = FakeS3()
    store = S3ThreadStore("b", client=client)
    _claim(store)
    for i in range(s3_module.COMPACT_AFTER + 3):
        _persist(
            store,
            f"op{i}",
            [{"role": "user", "content": str(i), "id": f"m{i}"}],
        )
    segments = [k for k in client.keys() if "/segments/" in k]
    assert len(segments) < s3_module.COMPACT_AFTER
    state = store.load_thread_state("sys", "t1")
    assert [m["id"] for m in state.messages] == [
        f"m{i}" for i in range(s3_module.COMPACT_AFTER + 3)
    ]


def test_local_write_to_a_temporal_owned_thread_is_refused():
    from gc_ua_temporalio.storage.sqlite import ProjectionConflict

    store = S3ThreadStore("b", client=FakeS3())
    _claim(store)
    with pytest.raises(ProjectionConflict):
        store.append_messages("sys", "t1", [{"role": "user", "content": "x"}])


# --- живой S3: MinIO в Docker ---------------------------------------------


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def minio_client():
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker недоступен")
    images = subprocess.run(
        [docker, "images", "-q", "minio/minio:latest"],
        capture_output=True,
        text=True,
    )
    if images.returncode != 0 or not images.stdout.strip():
        pytest.skip("образа minio/minio:latest нет локально")
    port = _free_port()
    name = f"gc-s3-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            docker,
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:9000",
            "-e",
            "MINIO_ROOT_USER=gcadmin",
            "-e",
            "MINIO_ROOT_PASSWORD=gcadmin-secret",
            "minio/minio:latest",
            "server",
            "/data",
        ],
        check=True,
        capture_output=True,
    )
    try:
        import boto3

        client = boto3.client(
            "s3",
            endpoint_url=f"http://127.0.0.1:{port}",
            aws_access_key_id="gcadmin",
            aws_secret_access_key="gcadmin-secret",
            region_name="us-east-1",
        )
        for _ in range(100):
            try:
                client.list_buckets()
                break
            except Exception:
                time.sleep(0.1)
        client.create_bucket(Bucket="threads")
        yield client
    finally:
        subprocess.run([docker, "rm", "-f", name], capture_output=True)


def test_s3_store_conforms_on_minio(minio_client):
    counter = itertools.count()
    passed = assert_durable_conforms(
        lambda: S3ThreadStore(
            "threads", f"conf-{next(counter)}", client=minio_client
        )
    )
    assert len(passed) == len(STRUCTURE_CHECKS) + len(BEHAVIOUR_CHECKS)


def test_minio_enforces_the_head_condition(minio_client):
    """Хранилище, которое молча игнорирует If-Match, потеряло бы записи —
    проверяем, что конфликт head действительно приходит от сервера."""
    store = S3ThreadStore("threads", "race", client=minio_client)
    _claim(store)
    base = store._base("sys", "t1")
    head, etag = store._get_head(base)
    _persist(store, "bump", [{"role": "user", "content": "x", "id": "a"}])
    from gc_ua_temporalio.storage.s3 import _HeadConflict

    with pytest.raises(_HeadConflict):
        store._put_head(base, head, etag)
