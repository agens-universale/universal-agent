"""Native deployment files use core interpolation without publishing credentials."""

import json
from dataclasses import asdict

from gc_ua_temporalio.native_contracts import NativeCall
from gc_ua_temporalio.native_provider import NativeWorkerConfig
from gc_ua_temporalio.native_worker import build_native_runtime


async def test_native_file_interpolates_credentials_before_validation_and_keeps_them_worker_only(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.chdir(tmp_path)
    secret = "unique-worker-secret-not-for-history"
    endpoint = "https://private-provider.example.test/chat"
    monkeypatch.setenv("NATIVE_TEST_TOKEN", secret)
    monkeypatch.setenv("NATIVE_TEST_URL", endpoint)
    monkeypatch.setenv("NATIVE_TEST_WORKSPACE", str(tmp_path))
    path = tmp_path / "native.json"
    raw = {
        "receipts_database": str(tmp_path / "service" / "receipts.sqlite"),
        "thread_stores": {"files": {"path": str(tmp_path / "threads")}},
        "profiles": {
            "test": {
                "revision": "v1",
                "workspace": "${ENV:NATIVE_TEST_WORKSPACE}",
                "core": {
                    "backend": {
                        "completions_url": "${ENV:NATIVE_TEST_URL}",
                        "chat_completion_headers": {
                            "Authorization": "Bearer ${ENV:NATIVE_TEST_TOKEN}"
                        },
                    },
                    "agent": {"use_local_subagents": False},
                    "logging": {"save_logs": False},
                },
            }
        },
    }
    path.write_text(json.dumps(raw))
    original = path.read_bytes()
    config = NativeWorkerConfig.load(path)
    assert config.profiles["test"].workspace == str(tmp_path)
    assert (
        config.profiles["test"].core.backend.chat_completion_headers[
            "Authorization"
        ]
        == "Bearer " + secret
    )
    provider, native, _ = build_native_runtime(config)
    try:
        call = NativeCall(
            "test", "v1", "owner", "prepare", {"store_id": "files"}
        )
        ctx = await provider(call)
        assert ctx.config_dict["completions_url"] == endpoint
        prepared = await native._prepare(call, ctx)
        encoded = json.dumps(asdict(prepared))
        assert secret not in encoded
        assert endpoint not in encoded
        assert "Authorization" not in encoded
        assert prepared.model_transport_configured
    finally:
        await provider.aclose()
    assert path.read_bytes() == original
    assert secret not in caplog.text


def test_native_missing_env_uses_same_core_policy(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.delenv("NATIVE_UNSET_TOKEN", raising=False)
    path = tmp_path / "native.json"
    path.write_text(
        json.dumps(
            {
                "receipts_database": "receipts.sqlite",
                "profiles": {
                    "test": {
                        "revision": "v1",
                        "workspace": str(tmp_path),
                        "core": {
                            "backend": {
                                "chat_completion_headers": {
                                    "X-Token": "${ENV:NATIVE_UNSET_TOKEN}"
                                }
                            }
                        },
                    }
                },
            }
        )
    )
    config = NativeWorkerConfig.load(path)
    assert (
        "X-Token"
        not in config.profiles["test"].core.backend.chat_completion_headers
    )
    assert "env var not set" in caplog.text
