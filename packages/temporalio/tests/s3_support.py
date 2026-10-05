"""MinIO в Docker для живых проверок S3 (без docker или образа — пропуск)."""

import shutil
import socket
import subprocess
import time
import uuid

import pytest

ACCESS_KEY = "gcadmin"
SECRET_KEY = "gcadmin-secret"


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def minio_server():
    """``(endpoint_url, client)`` поднятого MinIO с бакетом ``threads``."""
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
            f"MINIO_ROOT_USER={ACCESS_KEY}",
            "-e",
            f"MINIO_ROOT_PASSWORD={SECRET_KEY}",
            "minio/minio:latest",
            "server",
            "/data",
        ],
        check=True,
        capture_output=True,
    )
    try:
        import boto3

        endpoint = f"http://127.0.0.1:{port}"
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=SECRET_KEY,
            region_name="us-east-1",
        )
        for _ in range(100):
            try:
                client.list_buckets()
                break
            except Exception:
                time.sleep(0.1)
        client.create_bucket(Bucket="threads")
        yield endpoint, client
    finally:
        subprocess.run([docker, "rm", "-f", name], capture_output=True)


@pytest.fixture(scope="module")
def minio_client(minio_server):
    return minio_server[1]
