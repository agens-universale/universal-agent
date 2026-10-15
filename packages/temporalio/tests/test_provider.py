"""Production provider uses actual core preparation and explicit deployment scope."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from gc_core.config_modules import AppConfig
from gc_core.execution.resource_contracts import ResourceOwnerLost
from gc_core.execution.resource_host import WorkerResourceHost
from gc_core.settings import override_settings
from gc_core.threads.file_store import FileThreadStore
from gc_core_temporalio.contracts import ActivityCall
from gc_core_temporalio.provider import (
    AgentProfile,
    ContextProvider,
)
from gc_stores.sqlite import SQLiteThreadStore


def profile(tmp_path, **overrides):
    config = AppConfig.model_validate(
        {
            "agent": {"use_local_subagents": False},
            "logging": {"save_logs": False},
        }
    )
    return AgentProfile(workspace=str(tmp_path), core=config, **overrides)


def make_provider(tmp_path, profiles=None, store=None):
    store = store or FileThreadStore(str(tmp_path / "threads"))
    return ContextProvider(
        profiles=profiles or {"test": profile(tmp_path)},
        stores={"files": store},
        resource_host=WorkerResourceHost("test"),
    ), store


def call(**overrides):
    return replace(
        ActivityCall("test", "owner", "prepare", {"store_id": "files"}),
        **overrides,
    )


@pytest.mark.parametrize("backend", ["file", "sqlite"])
async def test_actual_context_store_and_stateless_restart(
    tmp_path, monkeypatch, backend
):
    monkeypatch.chdir(tmp_path)
    store = (
        FileThreadStore(str(tmp_path / "threads"))
        if backend == "file"
        else SQLiteThreadStore(str(tmp_path / "threads.sqlite"))
    )
    provider, _ = make_provider(tmp_path, store=store)
    before = Path.cwd()
    try:
        ctx, same = await asyncio.gather(provider(call()), provider(call()))
        assert ctx is same
        assert ctx.workspace.root == str(tmp_path)
        assert ctx.thread_store is store
        assert ctx.tools_data["fs_read_file"][
            "tool_fabric"
        ].__module__.startswith("gc_core.")
        assert await provider.resource_owner(call()) is None
        assert Path.cwd() == before
        await provider.close(call())
        assert await provider(call(operation_id="accounting")) is ctx
        restarted, _ = make_provider(tmp_path, store=store)
        try:
            fresh = await restarted(call(operation_id="retry"))
            assert (
                fresh is not ctx and fresh.workspace.root == ctx.workspace.root
            )
        finally:
            await restarted.aclose()
    finally:
        await provider.aclose()


async def test_background_owner_is_lazy_and_fresh_after_restart(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    provider, _ = make_provider(tmp_path)
    restarted = None
    try:
        ctx = await provider(call())
        assert await provider.resource_owner(call()) is None
        ref = await provider.ensure_resource_owner(call())
        assert await provider.resource_owner(call()) == ref
        assert await provider.ensure_resource_owner(call()) == ref
        assert await provider(call()) is ctx
        restarted, _ = make_provider(tmp_path)
        assert await restarted(call()) is not ctx  # stateless work survives
        # После перезапуска процесса — новый владелец: ресурсы не переживают
        # процесс, и модель узнаёт о потере из ошибки инструмента.
        fresh = await restarted.ensure_resource_owner(call())
        assert fresh.process_epoch != ref.process_epoch
        await provider.close(call())
        await provider.close(call())
        assert await provider(call()) is ctx
        with pytest.raises(ResourceOwnerLost):
            await provider.ensure_resource_owner(call())
    finally:
        await provider.aclose()
        if restarted:
            await restarted.aclose()


async def test_store_binding_pinned_profile_settings_follow_worker(
    tmp_path, monkeypatch
):
    """Как pydantic-ai: workflow знает профиль по имени, настройки — у воркера.

    Смена настроек или токена при перезапуске воркера не ломает сессии;
    закреплены только store и владелец контекста.
    """
    monkeypatch.chdir(tmp_path)
    provider, _ = make_provider(tmp_path)
    restarted = None
    try:
        with pytest.raises(ValueError, match="store"):
            await provider(call(data={"store_id": "other"}))
        ctx = await provider(call())
        original = profile(tmp_path)
        changed = original.model_copy(
            update={
                "core": override_settings(
                    original.core, {"agent.model_steering": "new behavior"}
                )
            }
        )
        restarted, _ = make_provider(tmp_path, {"test": changed})
        fresh = await restarted(call())
        assert fresh.settings.agent.model_steering == "new behavior"
        assert await provider(call()) is ctx
    finally:
        await provider.aclose()
        if restarted is not None:
            await restarted.aclose()


def test_wrong_workspace_rejected_without_chdir(tmp_path):
    before = Path.cwd()
    with pytest.raises(ValueError, match="start worker in workspace"):
        make_provider(tmp_path)
    assert Path.cwd() == before


async def test_actual_mcp_discovery_occurs_inside_resource_owner(
    tmp_path, monkeypatch
):
    import sys

    import gc_core_temporalio.provider as module

    monkeypatch.chdir(tmp_path)
    base = profile(tmp_path)
    configured = base.model_copy(
        update={
            "core": base.core.model_copy(
                update={
                    "mcp": AppConfig.model_validate(
                        {
                            "logging": {"save_logs": False},
                            "mcp": {
                                "servers": {
                                    "counter": {
                                        "transport": "stdio",
                                        "persistent_session": True,
                                        "command": sys.executable,
                                        "args": [
                                            str(
                                                Path(__file__).with_name(
                                                    "mcp_fixture_server.py"
                                                )
                                            ),
                                            "stdio",
                                            "0",
                                        ],
                                    }
                                }
                            },
                        }
                    ).mcp
                }
            )
        }
    )
    events = []
    actual = module.acreate_run_context

    async def create(*args, **kwargs):
        events.append(asyncio.current_task())
        return await actual(*args, **kwargs)

    monkeypatch.setattr(module, "acreate_run_context", create)
    provider, _ = make_provider(tmp_path, {"test": configured})
    try:
        ctx = await provider(call())
        ref = await provider.resource_owner(call())
        assert ref is not None
        assert events == [provider.resource_host._owners["owner"]._task]
        assert events[0] is not asyncio.current_task()
        assert any(name.endswith("increment") for name in ctx.tools_data)
        await provider.close(call())
        assert await provider(call()) is ctx
    finally:
        await provider.aclose()


@pytest.mark.parametrize("live", [False, True])
async def test_child_uses_real_runner_and_parent_resource_owner(
    tmp_path, monkeypatch, live
):
    monkeypatch.chdir(tmp_path)
    base = profile(tmp_path)
    configured = base.model_copy(
        update={
            "core": override_settings(
                base.core,
                {
                    "agent.use_local_subagents": True,
                    "backend.api_mode": "chat_completions",
                },
            )
        }
    )
    provider, _ = make_provider(tmp_path, {"test": configured})
    try:
        parent = await provider(call())
        ref = await provider.ensure_resource_owner(call()) if live else None
        child_call = call(
            owner_id="child",
            resource_owner=ref,
            child_context={
                "parent_owner_id": "owner",
                "parent_context": None,
                "tool_name": "subagent_worker",
                "frozen_level": "default",
                "force_legacy": False,
                "circuit_breaker": {},
            },
        )
        child = await provider(child_call)
        assert child is not parent
        assert child.os_backend is parent.os_backend
        assert child.mcp_session_owner == parent.mcp_session_owner
        assert child.run_id == "child"
        assert child._parent_context.workspace is parent.workspace
        assert await provider.resource_owner(child_call) == ref
        await provider.close(child_call)
        if ref is not None:
            assert await provider.ensure_resource_owner(call()) == ref
        await provider.release(call())
        assert (
            not provider._contexts
            and not provider._refs
            and not provider._parents
        )
        assert not provider.resource_host._owners
    finally:
        await provider.aclose()


async def test_poststeps_release_drops_context_logger_and_owner_cache(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    provider, _ = make_provider(tmp_path)
    released = []
    monkeypatch.setattr(
        "gc_core.utils.logging_module.release_run_logger", released.append
    )
    ctx = await provider(call())
    await provider.ensure_resource_owner(call())
    await provider.close(call())
    assert await provider(call(operation_id="accounting")) is ctx
    await provider.release(call(operation_id="release"))
    await provider.release(call(operation_id="release"))
    assert released == [ctx.logger]
    assert (
        not provider._contexts and not provider._refs and not provider._locks
    )
    await provider.aclose()


@pytest.mark.parametrize("operation", ["close", "release"])
async def test_cleanup_retry_on_restarted_worker_never_rebuilds_context(
    tmp_path, monkeypatch, operation
):
    """Повтор close/release после падения воркера (квитанций нет — операция
    исполняется снова) не собирает контекст заново и отвечает успехом."""
    from gc_core_temporalio.activities import AgentActivities
    from gc_core_temporalio.public_config import public_config_snapshot

    monkeypatch.chdir(tmp_path)
    provider, store = make_provider(tmp_path)
    await provider(call())
    await provider.ensure_resource_owner(call())

    def activities(context_provider):
        return AgentActivities(
            context_provider=context_provider,
            stores={"files": store},
            public_config=public_config_snapshot,
        )

    request = call(operation_id=operation)
    assert await getattr(activities(provider), operation)(request) is True
    restarted, _ = make_provider(tmp_path, store=store)

    async def forbidden(_call):
        raise AssertionError("Cleanup retry must not reconstruct a context")

    # If AgentActivities resolves this provider, its inherited __call__ would
    # otherwise rebuild a stateless context. Make the attempted resolution loud.
    class CleanupOnly:
        close = restarted.close
        release = restarted.release
        __call__ = staticmethod(forbidden)

    try:
        restored = activities(CleanupOnly())
        assert await getattr(restored, operation)(request) is True
        assert await getattr(restored, operation)(request) is True
        assert not restarted._contexts
    finally:
        await provider.aclose()
        await restarted.aclose()


async def test_worker_never_records_agent_edits(tmp_path, monkeypatch):
    """Журнал правок в durable-исполнении выключен при любом профиле: обёртки
    записи на бэкенде нет, и тулы правок не пишут журнал на воркере."""
    from gc_core.agent_edits.ctx import recording_enabled
    from gc_core.agent_edits.tracking import TrackingFsBackend

    monkeypatch.chdir(tmp_path)
    core = override_settings(
        profile(tmp_path).core,
        {
            "agent_edits.record.tui": True,
            "agent_edits.record.cli": True,
            "agent_edits.record.server": True,
        },
    )
    pinned = AgentProfile(workspace=str(tmp_path), core=core)
    provider, _ = make_provider(tmp_path, profiles={"test": pinned})
    try:
        ctx = await provider(call())
        assert not isinstance(ctx.os_backend, TrackingFsBackend)
        assert ctx.settings.agent_edits.record.model_dump() == {
            "tui": False,
            "cli": False,
            "server": False,
        }
        # Даже если режим запуска объявят позже, записывать нечего.
        ctx.runtime = ctx.runtime.with_values(run_mode="server")
        assert recording_enabled(ctx) is False
    finally:
        await provider.aclose()


async def test_worker_state_inside_workspace_is_protected(
    tmp_path, monkeypatch
):
    """Служебное состояние по умолчанию — вне workspace; заданное внутри —
    защищено от агента (история и payload — не его файлы)."""
    from gc_core_temporalio.provider import WorkerConfig

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    config = WorkerConfig.model_validate(
        {
            "state_dir": str(tmp_path / "state"),
            "profiles": {},
        }
    )
    assert all(
        not path.startswith(str(workspace)) for path in config.state_paths()
    )
    inside = str(workspace / "threads")
    provider = ContextProvider(
        profiles={"test": profile(workspace)},
        stores={"files": FileThreadStore(inside)},
        resource_host=WorkerResourceHost("test"),
        state_paths=[inside],
    )
    try:
        ctx = await provider(call())
        assert inside in ctx.settings.security.protected_paths.extra
    finally:
        await provider.aclose()
    # База SQLite-хранилища тредов — вместе с -wal/-shm.
    database = str(tmp_path / "state" / "threads.sqlite")
    paths = WorkerConfig.model_validate(
        {
            "thread_stores": {"files": {"kind": "sqlite", "path": database}},
            "profiles": {},
        }
    ).state_paths()
    assert database + "-wal" in paths and database + "-shm" in paths
    from gc_core.security.protected_paths import protected_decision

    assert (
        protected_decision(database + "-wal", cwd=str(workspace), extra=paths)
        == "deny"
    )


async def test_worker_logs_go_to_process_logging_unless_profile_says_files(
    tmp_path, monkeypatch
):
    """Под Temporal лог прогона по умолчанию — не файлом на диске воркера, а в
    logging процесса (propagate) и обработчики плагина; явное
    ``save_logs: true`` профиля — по-прежнему файл."""
    import logging

    monkeypatch.chdir(tmp_path)
    default = AgentProfile(
        workspace=str(tmp_path),
        core=AppConfig.model_validate(
            {"agent": {"use_local_subagents": False}}
        ),
    )
    explicit = AgentProfile(
        workspace=str(tmp_path),
        core=AppConfig.model_validate(
            {
                "agent": {"use_local_subagents": False},
                "logging": {"save_logs": True},
                "paths": {"logs_dump_dir": str(tmp_path / "logs")},
            }
        ),
    )
    records = []

    class _Sink(logging.Handler):
        def emit(self, record):
            records.append(record)

    store = FileThreadStore(str(tmp_path / "threads"))
    provider = ContextProvider(
        profiles={"test": default, "files": explicit},
        stores={"files": store},
        resource_host=WorkerResourceHost("test"),
        log_handlers=[_Sink()],
    )
    try:
        assert (
            provider._profiles["test"][1].settings.logging.save_logs is False
        )
        assert provider._profiles["test"][1].settings.logging.propagate is True
        # Лог уходит в общий logging процесса — секреты маскируются.
        assert (
            provider._profiles["test"][
                1
            ].settings.logging.redact_secrets.enabled
            is True
        )
        assert (
            provider._profiles["files"][1].settings.logging.save_logs is True
        )
        ctx = await provider(call())
        ctx.logger.info("hello")
        assert any(r.getMessage() == "hello" for r in records)
        assert not Path(ctx.logging_path).exists()
    finally:
        await provider.aclose()


async def test_store_without_thread_files_disables_view_image(
    tmp_path, monkeypatch
):
    """Хранилище без файлов треда и без каталога тредов: снимки
    ``fs_view_image`` легли бы на диск воркера — инструмент выключен."""
    from gc_testkit.memory_thread_store import MemoryThreadStore

    class _NoFiles(MemoryThreadStore):
        write_thread_file = None
        read_thread_file = None

    monkeypatch.chdir(tmp_path)
    for store, disabled in (
        (_NoFiles(), True),
        (MemoryThreadStore(), False),
        (FileThreadStore(str(tmp_path / "threads")), False),
    ):
        provider, _ = make_provider(tmp_path, store=store)
        stop = provider._profiles["test"][1].settings.agent.stop_tools
        assert ("fs_view_image" in stop) is disabled, type(store)
        await provider.aclose()


async def test_worker_without_human_has_no_ask_tool_and_no_ask_modes(
    tmp_path, monkeypatch
):
    """По умолчанию человека нет (``human_input: false``): инструмента
    вопроса у агента нет, режим ``ask`` охраны — ``deny``, явный ``allow``
    профиля не трогается. С человеком — профиль как есть."""
    monkeypatch.chdir(tmp_path)
    core = AppConfig.model_validate(
        {
            "agent": {"use_local_subagents": False},
            "logging": {"save_logs": False},
            "security": {"url_policy": {"mode": "allow"}},
        }
    )
    pinned = AgentProfile(workspace=str(tmp_path), core=core)
    store = FileThreadStore(str(tmp_path / "threads"))
    for human_input, expected in ((False, "deny"), (True, "ask")):
        provider = ContextProvider(
            profiles={"test": pinned},
            stores={"files": store},
            resource_host=WorkerResourceHost("test"),
            human_input=human_input,
        )
        settings = provider._profiles["test"][1].settings
        security = settings.security
        assert security.write_guard.mode == expected
        assert security.read_guard.mode == expected
        assert security.command_moderation.mode == expected
        assert security.url_policy.mode == "allow"
        assert ("turn_ask_human" in settings.agent.stop_tools) is (
            not human_input
        )
        await provider.aclose()


async def test_project_memory_is_off_under_temporal(tmp_path, monkeypatch):
    """Project memory — файл в рабочей папке, возможность TUI и CLI: под
    Temporal её нет при любом профиле, с человеком или без."""
    monkeypatch.chdir(tmp_path)
    store = FileThreadStore(str(tmp_path / "threads"))
    for human_input in (False, True):
        provider = ContextProvider(
            profiles={"test": profile(tmp_path)},
            stores={"files": store},
            resource_host=WorkerResourceHost("test"),
            human_input=human_input,
        )
        stop = provider._profiles["test"][1].settings.agent.stop_tools
        assert stop.count("project_memory") == 1
        await provider.aclose()


async def test_agent_store_reaches_the_worker_context(tmp_path, monkeypatch):
    """Хранилище агента приложения — в контексте воркера: одно на всех
    агентов или по агенту; лишнее имя и чужой объект — отказ на старте."""
    from gc_testkit.memory_agent_store import MemoryAgentStore

    monkeypatch.chdir(tmp_path)
    store = FileThreadStore(str(tmp_path / "threads"))
    agent_store = MemoryAgentStore()
    for value in (agent_store, {"test": agent_store}):
        provider = ContextProvider(
            profiles={"test": profile(tmp_path)},
            stores={"files": store},
            resource_host=WorkerResourceHost("test"),
            agent_store=value,
        )
        ctx = await provider(call())
        assert ctx.agent_store is agent_store
        await provider.close(call())
        await provider.aclose()
    with pytest.raises(ValueError, match="нет таких профилей"):
        ContextProvider(
            profiles={"test": profile(tmp_path)},
            stores={"files": store},
            resource_host=WorkerResourceHost("test"),
            agent_store={"other": agent_store},
        )
    with pytest.raises(TypeError, match="AgentStore"):
        ContextProvider(
            profiles={"test": profile(tmp_path)},
            stores={"files": store},
            resource_host=WorkerResourceHost("test"),
            agent_store=object(),
        )
