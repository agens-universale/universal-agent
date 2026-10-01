"""Remote API restart/reconnect and preview dedup, against a live workflow."""

import asyncio
import json
import shutil

import httpx
import pytest
from fastapi import FastAPI
from gc_ua_temporalio.config import TemporalRuntimeConfig
from gc_ua_temporalio.http_api import router
from gc_ua_temporalio.remote import RemoteCursor, RemoteSessionProvider
from gc_ua_temporalio.server_registry import TemporalRunRegistry
from provider_fixture import calls, finish, scripted_provider
from temporalio.testing import WorkflowEnvironment
from test_live_controls import Stand, until


def respond(data):
    if any(m["role"] == "tool" for m in data["messages"]):
        return finish()
    value = calls(
        (
            "remember",
            "session_memory",
            {
                "action": "append",
                "register": "knowledge",
                "content": "remote fact",
            },
        )
    )
    value["content"] = "Working on the remote request."
    return value


def make_api(config):
    registry = TemporalRunRegistry(config)
    app = FastAPI()
    app.include_router(router(registry))
    provider = RemoteSessionProvider(
        "http://test",
        "temporal",
        "remote",
        transport=httpx.ASGITransport(app=app),
    )
    return registry, provider


async def test_api_reconnect_and_server_restart_keep_running_session(tmp_path):
    with scripted_provider(respond) as (url, requests):
        async with await WorkflowEnvironment.start_local(
            dev_server_existing_path=shutil.which("temporal")
        ) as env:
            stand = Stand(tmp_path, env, url)
            config = json.loads(stand.config.read_text())
            config["profiles"]["demo"]["preview_stream"] = True
            stand.config.write_text(json.dumps(config))
            process = stand.start("before_tool")
            config = TemporalRuntimeConfig.load(stand.config)
            registry, first = make_api(config)
            try:
                for message_id, text in (
                    ("", "hello"),
                    ("empty", " "),
                    ("long", "x" * 16001),
                ):
                    with pytest.raises(httpx.HTTPStatusError) as invalid:
                        await first.send(message_id, text)
                    assert invalid.value.response.status_code == 400
                assert registry.views.owner("temporal", "remote") is None
                receipt = await first.send("message-1", "remember fact")
                handle = env.client.get_workflow_handle(receipt["workflow_id"])
                async with asyncio.timeout(40):
                    while not (tmp_path / "checkpoint").exists():
                        await asyncio.sleep(0.1)
                events = await first.events()
                assert any(e["kind"] == "preview" for e in events)
                await first.aclose()
                assert (await handle.describe()).status.name == "RUNNING"
                # Drop all API client/registry state; reconnect to the same durable owner.
                registry, second = make_api(config)
                duplicate = await second.send("message-1", "remember fact")
                assert duplicate["receipt"]["message_id"] == "message-1"
                assert len(requests) == 1
                with pytest.raises(httpx.HTTPStatusError) as conflict:
                    await second.send("message-1", "different text")
                assert conflict.value.response.status_code == 409
                process.kill()
                process.wait()
                stand.start()
                await until(handle, lambda s: s["phase"] == "idle")
                cursor = RemoteCursor()
                page = await second.history()
                assert cursor.apply_history(page)
                assert not cursor.apply_history(page)
                cursor.apply_events(await second.events())
                assert not cursor.previews
                metrics = await second.request("GET", "metrics")
                assert metrics["projection_lag"] == 0
                assert metrics["effects_unknown"] == 0
                assert metrics["phase"] == "idle"
                assert (await registry.legacy_status("temporal", "remote"))[
                    "status"
                ] == "completed"
                logs = await registry.legacy_logs("temporal", "remote")
                assert logs["messages"] == [
                    item["message"] for item in page["messages"]
                ]
                before = registry.views.store.load_meta("temporal", "remote")
                await second.request(
                    "PATCH",
                    "ui",
                    json={"title": "Remote session", "pinned": True},
                )
                assert (
                    registry.views.store.load_meta("temporal", "remote")
                    == before
                )
                assert (await second.history())["ui_meta"] == {
                    "title": "Remote session",
                    "pinned": True,
                }
                with pytest.raises(httpx.HTTPStatusError):
                    await second.request("PATCH", "ui", json={"steps": 0})
                await second.close_session()
                result = await handle.result()
                assert result["memory"]["knowledge"] == "remote fact"
                assert len(requests) == 2
                await second.aclose()
            finally:
                await first.aclose()
                stand.close()


def test_preview_retry_and_reconnect_never_duplicate_or_override_committed():
    cursor = RemoteCursor()

    def event(seq, attempt, kind, text=""):
        return {
            "cursor": seq,
            "operation": "model-1",
            "attempt": attempt,
            "kind": kind,
            "payload": {"text": text},
        }

    initial = [event(1, 1, "reset"), event(2, 1, "preview", "partial")]
    cursor.apply_events(initial)
    cursor.apply_events(initial)
    assert cursor.previews == {"model-1": "partial"}
    cursor.apply_events(
        [
            event(3, 2, "reset"),
            event(4, 2, "preview", "fresh"),
            event(5, 1, "preview", "late"),
        ]
    )
    assert cursor.previews == {"model-1": "fresh"}
    cursor.apply_events(
        [event(6, 2, "committed"), event(7, 2, "preview", "too late")]
    )
    assert not cursor.previews


def test_preview_storage_failure_does_not_fail_the_operation(
    tmp_path, monkeypatch
):
    import sqlite3
    from types import SimpleNamespace

    from gc_ua_temporalio.preview import PreviewSink, commit_preview
    from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore
    from gc_ua_temporalio.storage.views import SessionViews

    def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    runtime = SimpleNamespace(
        store=SQLiteThreadStore(tmp_path / "preview.sqlite")
    )
    monkeypatch.setattr(SessionViews, "emit", unavailable)
    sink = PreviewSink(runtime, "owner", "operation", attempt=1)
    sink.delta("mcp_progress", "half complete")
    commit_preview(runtime, "owner", "operation")
