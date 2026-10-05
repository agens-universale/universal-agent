"""Воркер с хранилищем тредов S3 (MinIO): тред живёт в бакете, а не на диске воркера.

Собирается так же, как в проде: ``NativeWorkerConfig`` → ``build_native_runtime``
→ ``worker_options``. Модель — скриптованный HTTP. После хода сверяется дерево
файлов: всё, что воркер написал локально, должно быть известным служебным
файлом, а не историей или метаданными треда.
"""

import asyncio
import os
import shutil
from pathlib import Path

import pytest
from gc_core.config_modules import AppConfig, env_vars
from gc_core.config_modules.env_vars import EnvVars
from gc_ua_temporalio.native_contracts import NativeRunRequest
from gc_ua_temporalio.native_provider import NativeWorkerConfig
from gc_ua_temporalio.native_worker import build_native_runtime, worker_options
from gc_ua_temporalio.native_workflow import NativeAgentWorkflow
from gc_ua_temporalio.storage.s3 import S3ThreadStore
from live_support import (
    provider,  # noqa: F401  # shared HTTP provider fixture
)
from s3_support import (  # noqa: F401  # shared MinIO fixtures
    ACCESS_KEY,
    SECRET_KEY,
    minio_client,
    minio_server,
)
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

QUEUE = "native-s3"


def _files(root: Path) -> set[str]:
    return {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file()
    }


async def test_native_thread_lives_only_in_s3(
    tmp_path,
    provider,  # noqa: F811
    minio_server,  # noqa: F811
    monkeypatch,
):
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("Temporal CLI is required")
    endpoint, client = minio_server
    url, requests = provider
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", ACCESS_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRET_KEY)
    # Служебные каталоги gc (state/cache/config) — внутрь tmp, чтобы аудит
    # видел каждую запись, а не только workspace.
    gc_home = tmp_path / "gc-home"
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=gc_home))
    # Скриптованный провайдер читает `<tmp_path>/example.txt` — workspace тот же.
    workspace = tmp_path
    (workspace / "example.txt").write_text("file from native workflow")
    monkeypatch.chdir(workspace)
    core = AppConfig.model_validate(
        {
            "backend": {
                "api_mode": "chat_completions",
                "completions_url": url,
                "chat_completion_kwargs": {"stream": False},
            },
            "agent": {
                "model": "scripted",
                "max_steps": 10,
                "use_local_subagents": False,
                "strict_turn_finish_validation": False,
            },
            "logging": {"save_logs": False, "console_trace": False},
            "tui": {"text_answer_mode": "deny"},
        }
    )
    config = NativeWorkerConfig.model_validate(
        {
            "workflow_queue": QUEUE,
            "resource_queue": QUEUE,
            "model_queue": QUEUE,
            "receipts_database": str(tmp_path / "worker" / "receipts.sqlite"),
            "thread_stores": {
                "s3": {
                    "kind": "s3",
                    "bucket": "threads",
                    "prefix": "e2e",
                    "endpoint_url": endpoint,
                    "region": "us-east-1",
                }
            },
            "profiles": {
                "live": {
                    "revision": "v1",
                    "workspace": str(workspace),
                    "store_id": "s3",
                    "core": core,
                }
            },
        }
    )
    before = _files(tmp_path)
    contexts, native, threads = build_native_runtime(config)
    try:
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=binary
        ) as env:
            (options,) = worker_options(config, native, threads)
            async with Worker(env.client, **options):
                handle = await env.client.start_workflow(
                    NativeAgentWorkflow.run,
                    NativeRunRequest(
                        "live",
                        "v1",
                        "s3",
                        "test",
                        "s3-thread",
                        [
                            f"Read {workspace}/example.txt and record the result."
                        ],
                        QUEUE,
                        QUEUE,
                    ),
                    id="native-s3",
                    task_queue=QUEUE,
                )
                result = await asyncio.wait_for(handle.result(), 60)
                history = await handle.fetch_history()
        await Replayer(workflows=[NativeAgentWorkflow]).replay_workflow(
            history
        )
    finally:
        await contexts.aclose()

    assert result is not None
    assert requests, "модель вызывалась"
    store = S3ThreadStore("threads", "e2e", client=client)
    messages = store.load_messages("test", "s3-thread")
    meta = store.load_meta("test", "s3-thread")
    assert any(m.get("type") == "function_call_output" for m in messages)
    assert any(
        "file from native workflow" in str(m.get("output", ""))
        for m in messages
    ), "результат fs_read_file сохранён в S3"
    assert meta.get("messages_count") == len(messages)
    assert store.read_snapshot("test", "s3-thread").owner_id is None

    written = sorted(_files(tmp_path) - before)
    thread_files = [
        path
        for path in written
        if "/threads/" in f"/{path}"
        or os.path.basename(path).startswith(("data_", "meta_", "edits_"))
        or "/.store/" in f"/{path}"
    ]
    assert thread_files == [], f"тред записан мимо S3: {thread_files}"
    allowed = {
        "worker/receipts.sqlite",
        "worker/receipts.sqlite-wal",
        "worker/receipts.sqlite-shm",
    }
    assert set(written) <= allowed, f"неожиданные локальные файлы: {written}"
