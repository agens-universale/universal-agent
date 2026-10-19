"""``gc-stores sync``: ресурсы агента из каталога и умолчания пакета в
хранилище агента."""

from gc_core.agent_store import FileAgentStore, Prompt
from gc_stores import sqlite
from gc_stores.defaults import bundled_source
from gc_stores.sync import copy_defaults


async def test_copy_defaults_puts_package_prompts_and_system_skills(tmp_path):
    store = sqlite.SQLiteAgentStore(tmp_path / "a.db", agent_id="a")
    report = await copy_defaults(store)
    bundled = await FileAgentStore().read("prompt", Prompt.SUMMARY)
    assert await store.read("prompt", Prompt.SUMMARY) == bundled
    assert "prompt:summary" in report.written
    assert any(name.startswith("skill:system/") for name in report.written)
    stored = await store.list_resources()
    assert {r.source for r in stored} == {bundled_source()}
    again = await copy_defaults(store)
    assert again.written == [] and len(again.unchanged) == len(report.written)


async def test_copy_defaults_keeps_a_users_prompt(tmp_path):
    store = sqlite.SQLiteAgentStore(tmp_path / "a.db", agent_id="a")
    await store.put_resource("prompt", Prompt.SUMMARY, b"own")
    report = await copy_defaults(store)
    assert await store.read("prompt", Prompt.SUMMARY) == b"own"
    assert "prompt:summary" not in report.written


async def test_copy_defaults_dry_run_writes_nothing(tmp_path):
    store = sqlite.SQLiteAgentStore(tmp_path / "a.db", agent_id="a")
    report = await copy_defaults(store, dry_run=True)
    assert report.written and await store.list_resources() == []
