"""Конформанс ``AgentStore`` v2: файловое и in-memory хранилища."""

import shutil

from gc_core.agent_store import FileAgentStore, ProjectKey
from gc_testkit.agent_store_conformance import PROJECT, Seed, assert_conforms
from gc_testkit.memory_agent_store import MemoryAgentStore
from gc_testkit.settings import make_settings


async def test_memory_agent_store_conforms():
    await assert_conforms(
        lambda seed: MemoryAgentStore(
            prompts={"system": seed.system_prompt},
            subagents=dict(seed.subagents),
            instructions=dict(seed.instructions),
            skills=dict(seed.skills),
            projects={
                PROJECT.id: {
                    "instructions": dict(seed.project_instructions),
                    "skill": dict(seed.project_skills),
                }
            },
            fallback=None,
        )
    )


def _write(root, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))


async def test_file_agent_store_conforms(tmp_path, monkeypatch):
    """Файловое: ресурсы агента — по путям конфига и корням слоёв, ресурсы
    проекта — в каталоге проекта (``ProjectKey.root``)."""
    from gc_core.agent_store import file_store
    from gc_core.instructions import layers
    from gc_core.tools.skills import registry

    agent = tmp_path / "agent"
    project = ProjectKey("conformance-project", str(tmp_path / "repo"))
    other = ProjectKey("conformance-other", str(tmp_path / "other"))
    monkeypatch.setattr(layers, "effective_config_dir", lambda: agent / "cfg")
    monkeypatch.setattr(
        registry,
        "_system_skills_root",
        lambda: str(agent / "skills" / "system"),
    )
    monkeypatch.setattr(
        registry, "_user_skills_root", lambda: str(agent / "skills" / "user")
    )
    monkeypatch.setattr(
        file_store,
        "memory_backup_path",
        lambda key: tmp_path / "backup" / key.id / "memory.md",
    )

    def make(seed: Seed) -> FileAgentStore:
        for path in (
            agent,
            tmp_path / "repo",
            tmp_path / "other",
            tmp_path / "backup",
        ):
            shutil.rmtree(path, ignore_errors=True)
        _write(agent / "agents", seed.subagents)
        _write(agent, {"system.md": seed.system_prompt})
        _write(agent / "cfg", {"AGENTS.md": seed.instructions["L1"]})
        _write(agent / "skills", seed.skills)
        repo = tmp_path / "repo"
        _write(repo / ".gc", {"AGENTS.md": seed.project_instructions["L2"]})
        _write(repo, {"AGENTS.md": seed.project_instructions["L3"]})
        _write(
            repo / ".gc" / "skills",
            {
                rel.split("/", 1)[1]: text
                for rel, text in seed.project_skills.items()
            },
        )
        (tmp_path / "other").mkdir()
        return FileAgentStore(
            make_settings(
                {
                    "paths.chat_completions_system_prompt_path": str(
                        agent / "system.md"
                    ),
                    "agent.subagents_dir": str(agent / "agents"),
                }
            )
        )

    await assert_conforms(make, project=project, other=other)
