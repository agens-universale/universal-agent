"""Конформанс ``AgentStore`` v2: файловое и in-memory хранилища."""

from gc_core.agent_store import FileAgentStore
from gc_testkit.agent_store_conformance import PROJECT, Seed, assert_conforms
from gc_testkit.memory_agent_store import MemoryAgentStore
from gc_testkit.settings import make_settings


async def test_memory_agent_store_conforms():
    await assert_conforms(
        lambda seed: MemoryAgentStore(
            prompts={"system": seed.system_prompt},
            subagents=dict(seed.subagents),
            fallback=None,
        )
    )


async def test_file_agent_store_conforms(tmp_path, monkeypatch):
    from gc_core.agent_store import file_store

    counter = iter(range(1000))

    def make(seed: Seed) -> FileAgentStore:
        root = tmp_path / f"case{next(counter)}"
        agents = root / "agents"
        agents.mkdir(parents=True)
        for name, text in seed.subagents.items():
            (agents / name).write_text(text, encoding="utf-8")
        system = root / "system.md"
        system.write_bytes(seed.system_prompt.encode("utf-8"))
        store = FileAgentStore(
            make_settings(
                {
                    "paths.chat_completions_system_prompt_path": str(system),
                    "agent.subagents_dir": str(agents),
                }
            )
        )
        # Память набора — в своём каталоге каждого случая, бэкап — рядом.
        monkeypatch.setattr(
            file_store,
            "memory_path",
            lambda project: root / ".gc" / "project_memory.md",
        )
        monkeypatch.setattr(
            file_store,
            "memory_backup_path",
            lambda project: root / "backup.md",
        )
        assert PROJECT.id
        return store

    await assert_conforms(make)
