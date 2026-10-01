"""Native remote pages use real core storage, including replacement history."""

import pytest
from gc_core.thread_store_file import FileThreadStore
from gc_ua_temporalio.native_views import NativeViews
from gc_ua_temporalio.remote import RemoteCursor
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore


@pytest.fixture(params=["file", "sqlite"])
def views(tmp_path, request):
    store = (
        FileThreadStore(str(tmp_path / "threads"))
        if request.param == "file"
        else SQLiteThreadStore(str(tmp_path / "threads.sqlite"))
    )
    return NativeViews(
        store, SQLiteThreadStore(str(tmp_path / "receipts.sqlite"))
    )


def test_native_history_replacement_resets_prefix_without_meta_reset(views):
    store = views.store
    store.append_messages(
        "s", "t", [{"id": "one", "role": "user", "content": "first"}]
    )
    first = views.history("s", "t")
    assert first["messages"][0]["message"]["content"] == "first"
    views.patch_ui("s", "t", {"title": "Title"})
    unchanged = views.history(
        "s", "t", after=first["after"], token=first["token"]
    )
    assert not unchanged["reset"]
    assert unchanged["messages"] == []
    assert unchanged["ui_meta"]["title"] == "Title"
    store.replace_messages(
        "s", "t", [{"id": "two", "role": "user", "content": "replacement"}]
    )
    changed = views.history(
        "s", "t", after=first["after"], token=first["token"]
    )
    assert changed["reset"]
    cursor = RemoteCursor()
    cursor.apply_history(first)
    assert (
        cursor.apply_history(changed)[0]["message"]["content"] == "replacement"
    )


def test_native_empty_history_and_input_validation(views):
    assert views.history("s", "empty")["messages"] == []
    with pytest.raises(ValueError):
        views.history("s", "t", after=-1)
    with pytest.raises(ValueError):
        views.patch_ui("s", "t", {"runtime": {}})


@pytest.mark.parametrize("human", [False, True])
async def test_native_remote_api_real_workflow_reconnect_and_close(
    tmp_path, monkeypatch, human
):
    import asyncio
    import shutil

    import httpx
    from fastapi import FastAPI
    from gc_core.config_modules import AppConfig
    from gc_ua_temporalio.http_api import router
    from gc_ua_temporalio.native_provider import (
        NativeProfile,
        NativeStoreConfig,
        NativeWorkerConfig,
    )
    from gc_ua_temporalio.native_registry import (
        NativeRemoteClient,
        NativeRunRegistry,
    )
    from gc_ua_temporalio.native_worker import (
        build_native_runtime,
        worker_options,
    )
    from gc_ua_temporalio.remote import RemoteSessionProvider
    from provider_fixture import calls, finish, scripted_provider
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("Temporal CLI is required")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    with scripted_provider(
        lambda _: (
            calls(("ask", "turn_ask_human", {"question": "Detail?"}))
            if human and len(requests) == 1
            else {**finish(), "content": "Visible preview"}
        )
    ) as (url, requests):
        config = NativeWorkerConfig(
            workflow_queue="remote-native",
            model_queue="remote-native",
            resource_queue="remote-native",
            receipts_database=str(tmp_path / "receipts.sqlite"),
            thread_stores={
                "files": NativeStoreConfig(path=str(tmp_path / "threads"))
            },
            profiles={
                "test": NativeProfile(
                    revision="v1",
                    preview_stream=human,
                    workspace=str(workspace),
                    core=AppConfig.model_validate(
                        {
                            "backend": {
                                "api_mode": "chat_completions",
                                "completions_url": url,
                                "stream": human,
                                "chat_completion_kwargs": {"stream": human},
                            },
                            "agent": {
                                "model": "scripted",
                                "max_steps": 3,
                                "strict_turn_finish_validation": False,
                                "use_local_subagents": False,
                            },
                            "logging": {
                                "save_logs": False,
                                "console_trace": False,
                            },
                            "tui": {
                                "text_answer_mode": "deny",
                                "stream": human,
                            },
                        }
                    ),
                )
            },
        )
        provider, native, threads = build_native_runtime(config)
        try:
            async with await WorkflowEnvironment.start_local(
                dev_server_existing_path=binary
            ) as env:

                def remote():
                    registry = NativeRunRegistry(config, profile="test")
                    registry._client = NativeRemoteClient(
                        env.client, workflow_queue=config.workflow_queue
                    )
                    app = FastAPI()
                    app.include_router(router(registry))
                    return registry, RemoteSessionProvider(
                        "http://test",
                        "test",
                        "remote",
                        transport=httpx.ASGITransport(app=app),
                    )

                async with Worker(
                    env.client,
                    **worker_options(config, native, threads)[0],
                    workflow_failure_exception_types=[Exception],
                ):
                    registry, first = remote()
                    receipt = await first.send("message-1", "Hello")
                    handle = env.client.get_workflow_handle(
                        receipt["workflow_id"]
                    )
                    if human:
                        async with asyncio.timeout(40):
                            while not (status := await first.status()).get(
                                "pending_human"
                            ):
                                await asyncio.sleep(0.05)
                        with pytest.raises(httpx.HTTPStatusError) as stale:
                            await first.stop("stale-turn")
                        assert stale.value.response.status_code == 409
                        request_id = status["pending_human"]["request_id"]
                        assert await first.answer(
                            request_id, "reply-1", "Detail"
                        )
                    async with asyncio.timeout(40):
                        while (await first.status())["phase"] != "idle":
                            await asyncio.sleep(0.05)
                    first_page = await first.history()
                    assert first_page["messages"]
                    assert len(requests) == (2 if human else 1)
                    await first.aclose()
                    registry, second = remote()
                    assert (await second.status())[
                        "backend"
                    ] == "temporal-native"
                    duplicate = await second.send("message-1", "Hello")
                    assert duplicate["receipt"]["duplicate"]
                    if human:
                        assert await second.answer(
                            request_id, "reply-1", "Detail"
                        )
                    assert (await second.history())["messages"] == first_page[
                        "messages"
                    ]
                    events = await second.events()
                    assert any(
                        event["kind"] == "committed" for event in events
                    )
                    if human:
                        assert any(
                            event["kind"] == "preview" for event in events
                        )
                    await second.close_session()
                    await asyncio.wait_for(handle.result(), 20)
                    await second.aclose()
                    assert len(requests) == (2 if human else 1)
                    assert registry.views.store.load_meta("test", "remote")
        finally:
            await provider.aclose()


def test_native_events_cover_all_runs_without_history_projection(tmp_path):
    receipts = SQLiteThreadStore(str(tmp_path / "receipts.sqlite"))
    views = NativeViews(
        FileThreadStore(str(tmp_path / "threads")),
        receipts,
        workflow_id=lambda *_: "native-session",
    )
    views.technical.emit(
        "native-session/run-1/turn-1",
        "op-1",
        1,
        1,
        "preview",
        {"text": "first"},
    )
    views.technical.emit(
        "native-session/run-2/turn-2", "op-2", 1, 1, "committed", {}
    )
    views.technical.emit(
        "native-session-other/run-1",
        "op-3",
        1,
        1,
        "preview",
        {"text": "foreign"},
    )
    events = views.events("s", "t")
    assert [event["operation"] for event in events] == ["op-1", "op-2"]
    assert views.events("s", "t", after=events[0]["cursor"]) == events[1:]
    assert views.history("s", "t")["messages"] == []
