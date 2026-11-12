"""storage: в config.yml — ядро знает только схему; хранилища открывает фронтенд."""

import pytest
from gc_core.config_modules.field_meta import MASK, redacted_dump
from gc_core.run_context import acreate_run_context
from gc_core.stores import StorageNotOpenedError, require_opened_storage
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore
from gc_testkit.settings import make_settings
from pydantic import ValidationError


def test_files_by_default():
    storage = make_settings().settings.storage
    assert storage.part("threads") == ("files", {})
    assert storage.part("agent") == ("files", {})
    assert storage.ensure_schema is False
    assert storage.is_files()


def test_parts_carry_type_and_raw_params():
    storage = make_settings(
        {
            "storage": {
                "threads": {"type": "postgres", "dsn": "postgresql://h/db"},
                "agent": {"type": "files"},
                "ensure_schema": True,
            }
        }
    ).settings.storage
    assert storage.part("threads") == (
        "postgres",
        {"dsn": "postgresql://h/db"},
    )
    assert storage.part("agent") == ("files", {})
    assert storage.ensure_schema is True
    assert not storage.is_files()


@pytest.mark.parametrize(
    "storage",
    [
        {"threads": {"type": 5}},
        {"threads": {"type": ""}},
        {"thread": {"type": "postgres"}},
        {"threads": "postgres"},
    ],
)
def test_bad_storage_section_is_a_schema_error(storage):
    with pytest.raises(ValidationError):
        make_settings({"storage": storage})


def test_secrets_are_hidden_in_the_dump():
    dumped = redacted_dump(
        make_settings(
            {
                "storage": {
                    "threads": {
                        "type": "postgres",
                        "dsn": "postgresql://u:pw@h/db",
                    },
                    "agent": {
                        "type": "redis",
                        "url": "redis://:pw@h:6379/0",
                        "password": "pw",
                    },
                }
            }
        ).settings
    )
    threads, agent = dumped["storage"]["threads"], dumped["storage"]["agent"]
    assert threads["dsn"] == MASK
    assert threads["type"] == "postgres"
    assert "pw" not in agent["url"]
    assert agent["password"] == MASK


def test_non_file_storage_without_opened_store_is_refused():
    settings = make_settings(
        {"storage": {"threads": {"type": "postgres", "dsn": "x"}}}
    )
    with pytest.raises(StorageNotOpenedError) as err:
        require_opened_storage(settings, thread_store=None, agent_store=None)
    text = str(err.value)
    assert "storage.threads.type=postgres" in text
    assert "open_configured" in text
    # Открытое хранилище передано — конфиг его не перекрывает.
    require_opened_storage(
        settings, thread_store=MemoryThreadStore(), agent_store=None
    )


def test_agent_part_is_checked_separately():
    settings = make_settings({"storage": {"agent": {"type": "sqlite"}}})
    with pytest.raises(StorageNotOpenedError) as err:
        require_opened_storage(
            settings, thread_store=MemoryThreadStore(), agent_store=None
        )
    assert "storage.agent.type=sqlite" in str(err.value)


async def test_run_context_refuses_before_anything_starts():
    settings = make_settings(
        {"storage": {"threads": {"type": "postgres", "dsn": "x"}}}
    )
    with pytest.raises(StorageNotOpenedError):
        await acreate_run_context(settings)


def test_files_only_frontend_names_the_part_and_the_program():
    """TUI и ACP хранят только в файлах: нефайловая часть — отказ при старте
    с названием части и программы, а не падение на первом ходе."""
    from gc_core.stores import files_only_rejection

    assert files_only_rejection(make_settings(), program="gc-ua") is None
    text = files_only_rejection(
        make_settings(
            {"storage": {"agent": {"type": "sqlite", "path": "a.db"}}}
        ),
        program="gc-acp",
    )
    assert text is not None
    assert "gc-acp" in text and "storage.agent" in text and "sqlite" in text
