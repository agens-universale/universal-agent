"""``aopen_configured``: хранилища из ``storage:`` config.yml для фронтенда."""

import textwrap

import pytest
from gc_core.agent_store import FileAgentStore
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import LeaseRequest, ThreadKey
from gc_stores.configured import StorageConfigError, aopen_configured
from gc_testkit.settings import make_settings

KEY = ThreadKey(system="s", thread="t")


def _settings(tmp_path, storage: dict, **extra):
    return make_settings(
        {
            "storage": storage,
            "paths.threads_data_path": str(tmp_path / "threads"),
            **extra,
        }
    )


async def test_files_by_default(tmp_path):
    stores = await aopen_configured(_settings(tmp_path, {}))
    try:
        assert isinstance(stores.threads, FileThreadStore)
        assert isinstance(stores.agent, FileAgentStore)
    finally:
        await stores.aclose()


async def _seed_prompts(db: str, agent_id: str = "coder") -> None:
    """Промпты пакета — в хранилище агента (как gc-stores sync
    --with-defaults)."""
    from gc_stores import sqlite
    from gc_stores.sync import copy_defaults

    store = sqlite.SQLiteAgentStore(db, agent_id=agent_id)
    try:
        await copy_defaults(store)
    finally:
        await store.close()


async def test_agent_store_without_prompts_is_refused(tmp_path):
    """Без промптов ход упал бы посреди работы (сжатие, finish gate) —
    отказ уже при открытии, с подсказкой."""
    db = str(tmp_path / "agent.db")
    storage = {
        "agent": {"type": "sqlite", "path": db, "agent_id": "coder"},
        "ensure_schema": True,
    }
    with pytest.raises(StorageConfigError) as err:
        await aopen_configured(_settings(tmp_path, storage))
    assert "gc-stores sync --with-defaults" in str(err.value)
    assert "finish_gate" in str(err.value)
    await _seed_prompts(db)
    stores = await aopen_configured(_settings(tmp_path, storage))
    await stores.aclose()


async def test_same_database_shares_one_connection(tmp_path):
    db = str(tmp_path / "agent.db")
    await _seed_prompts(db)
    stores = await aopen_configured(
        _settings(
            tmp_path,
            {
                "threads": {"type": "sqlite", "path": db},
                "agent": {"type": "sqlite", "path": db, "agent_id": "coder"},
            },
        )
    )
    try:
        assert stores.threads._db is stores.agent._db
        record = await stores.threads.claim(KEY, LeaseRequest(owner="w"))
        assert record.lease is not None
    finally:
        await stores.aclose()


async def test_threads_in_database_agent_on_files(tmp_path):
    stores = await aopen_configured(
        _settings(
            tmp_path,
            {"threads": {"type": "sqlite", "path": str(tmp_path / "t.db")}},
        )
    )
    try:
        assert type(stores.threads).__name__ == "SQLiteThreadStore"
        assert isinstance(stores.agent, FileAgentStore)
    finally:
        await stores.aclose()


async def test_own_store_by_factory_path(tmp_path, monkeypatch):
    module = tmp_path / "my_store_mod.py"
    module.write_text(
        textwrap.dedent(
            """
            from gc_testkit.memory_thread_store_v2 import MemoryThreadStore

            SEEN = []

            def open_threads(params):
                SEEN.append(params)
                return MemoryThreadStore()
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    stores = await aopen_configured(
        _settings(
            tmp_path,
            {"threads": {"type": "my_store_mod:open_threads", "region": "x"}},
        )
    )
    import my_store_mod

    assert my_store_mod.SEEN == [{"region": "x"}]
    assert type(stores.threads).__name__ == "MemoryThreadStore"
    await stores.aclose()


@pytest.mark.parametrize(
    ("storage", "needle"),
    [
        ({"threads": {"type": "sqlite"}}, "path"),
        ({"threads": {"type": "sqlite", "path": "x", "dns": "y"}}, "dns"),
        ({"agent": {"type": "sqlite", "path": "x"}}, "agent_id"),
        ({"threads": {"type": "oracle"}}, "oracle"),
        ({"threads": {"type": "no_such_mod_xyz:open"}}, "no_such_mod_xyz"),
    ],
)
async def test_bad_storage_is_a_clear_error(tmp_path, storage, needle):
    with pytest.raises(StorageConfigError) as err:
        await aopen_configured(_settings(tmp_path, storage))
    assert needle in str(err.value)


async def test_missing_driver_names_the_extra(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_asyncpg(name, *args, **kwargs):
        if name == "asyncpg":
            raise ModuleNotFoundError(
                "No module named 'asyncpg'", name="asyncpg"
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_asyncpg)
    with pytest.raises(StorageConfigError) as err:
        await aopen_configured(
            _settings(
                tmp_path,
                {"threads": {"type": "postgres", "dsn": "postgresql://h/db"}},
            )
        )
    assert "gc-stores[postgres]" in str(err.value)


async def test_unmigrated_postgres_is_refused_or_migrated(
    tmp_path, postgres_dsn, schemas
):
    schema = schemas.name()
    storage = {
        "threads": {"type": "postgres", "dsn": postgres_dsn, "schema": schema},
        "agent": {
            "type": "postgres",
            "dsn": postgres_dsn,
            "schema": schema,
            "agent_id": "coder",
        },
    }
    with pytest.raises(StorageConfigError) as err:
        await aopen_configured(_settings(tmp_path, storage))
    assert "gc-stores migrate" in str(err.value)
    from gc_stores.configured import open_part
    from gc_stores.sync import copy_defaults

    migrated = _settings(tmp_path, {**storage, "ensure_schema": True})
    agent = open_part(migrated, "agent")
    try:
        await agent.ensure_schema()
        await copy_defaults(agent)
    finally:
        await agent.close()
    stores = await aopen_configured(migrated)
    try:
        await stores.agent.read("prompt", "system")
    finally:
        await stores.aclose()


def _config_yml(tmp_path, storage: dict):
    import yaml

    path = tmp_path / "config.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "storage": storage,
                "paths": {"threads_data_path": str(tmp_path / "threads")},
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def test_cli_migrate_and_sync_take_the_connection_from_config(
    tmp_path, capsys
):
    from gc_stores.cli import main

    db = str(tmp_path / "agent.db")
    config = _config_yml(
        tmp_path,
        {
            "threads": {"type": "sqlite", "path": db},
            "agent": {"type": "sqlite", "path": db, "agent_id": "coder"},
        },
    )
    assert main(["migrate", "--config", config]) == 0
    assert "версия схемы" in capsys.readouterr().out
    agent_dir = tmp_path / "agent"
    (agent_dir / "prompts").mkdir(parents=True)
    (agent_dir / "prompts" / "system.md").write_text("S", encoding="utf-8")
    assert main(["sync", str(agent_dir), "--config", config]) == 0
    assert "prompt:system" in capsys.readouterr().out


def test_cli_needs_store_or_config(capsys):
    from gc_stores.cli import main

    assert main(["migrate"]) == 2
    assert "--store" in capsys.readouterr().err


def test_cli_migrate_refuses_file_threads(tmp_path, capsys):
    from gc_stores.cli import main

    config = _config_yml(tmp_path, {})
    assert main(["migrate", "--config", config]) == 1
    assert "files" in capsys.readouterr().err
