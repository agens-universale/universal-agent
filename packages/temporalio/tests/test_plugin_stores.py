"""``AgentPlugin(stores=…)``: связка хранилищ — тот же путь, что
``thread_store=`` и ``agent_store=`` по отдельности."""

import pytest
from gc_core.stores import Stores
from gc_core.threads.file_store import FileThreadStore
from gc_core_temporalio.plugin import DEFAULT_STORE, AgentPlugin
from gc_testkit.memory_agent_store import MemoryAgentStore


def test_stores_reach_the_worker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stores = Stores(
        threads=FileThreadStore(str(tmp_path / "t")),
        agent=MemoryAgentStore(fallback=None),
    )
    plugin = AgentPlugin({"a": {}}, stores=stores)
    assert plugin.provider.stores[DEFAULT_STORE] is stores.threads
    assert plugin.provider.agent_stores["a"] is stores.agent


def test_stores_conflict_with_separate_stores(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stores = Stores(
        threads=FileThreadStore(str(tmp_path / "t")),
        agent=MemoryAgentStore(fallback=None),
    )
    with pytest.raises(ValueError, match="stores"):
        AgentPlugin(
            {"a": {}},
            stores=stores,
            thread_store=FileThreadStore(str(tmp_path / "u")),
        )
    with pytest.raises(ValueError, match="stores"):
        AgentPlugin(
            {"a": {}},
            stores=stores,
            agent_store=MemoryAgentStore(fallback=None),
        )
