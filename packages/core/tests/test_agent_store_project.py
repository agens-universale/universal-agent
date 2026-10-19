"""Проект в хранилище агента: какие ресурсы его требуют и как называется сам
проект (``agent.project_id`` или slug пути)."""

from types import SimpleNamespace

from gc_core.agent_store import (
    ProjectKey,
    needs_project,
    project_key_for,
    project_key_of,
)
from gc_testkit.settings import make_settings


def test_project_resources_are_named():
    assert needs_project("instructions", "L2")
    assert needs_project("instructions", "L3")
    assert not needs_project("instructions", "L1")
    assert not needs_project("instructions", "L0")
    assert needs_project("skill", "project")
    assert needs_project("skill", "project/x/SKILL.md")
    assert needs_project("skill", "/project/x")
    assert not needs_project("skill", "projects/x")
    assert not needs_project("skill", "user/x/SKILL.md")
    assert not needs_project("prompt", "system")
    assert not needs_project("subagent", "project")


def test_project_id_setting_wins_over_path(tmp_path):
    settings = make_settings({"agent.project_id": "billing"})
    assert project_key_for(str(tmp_path), settings) == ProjectKey(
        "billing", str(tmp_path)
    )


def test_without_setting_id_is_slug_of_path(tmp_path):
    key = project_key_for(str(tmp_path), make_settings({}))
    assert key.root == str(tmp_path)
    assert key.id.endswith(tmp_path.name.lower())


def test_without_settings_id_is_slug_of_path(tmp_path):
    assert project_key_for(str(tmp_path)).id.endswith(tmp_path.name.lower())


def test_project_key_of_context_reads_setting(tmp_path):
    ctx = SimpleNamespace(
        project_gc_dir=tmp_path / ".gc",
        settings=make_settings({"agent.project_id": "p1"}),
    )
    key = project_key_of(ctx)
    assert key == ProjectKey("p1", str(tmp_path))


async def test_file_store_reads_project_resources_from_project_root(tmp_path):
    import pytest
    from gc_core.agent_store import FileAgentStore

    root = tmp_path / "repo"
    (root / ".gc" / "skills" / "s").mkdir(parents=True)
    (root / ".gc" / "AGENTS.md").write_text("L2 текст", encoding="utf-8")
    (root / "AGENTS.md").write_text("L3 текст", encoding="utf-8")
    (root / ".gc" / "skills" / "s" / "SKILL.md").write_text(
        "---\nname: s\n---\n", encoding="utf-8"
    )
    store = FileAgentStore(make_settings({}))
    project = ProjectKey("p", str(root))
    assert await store.read("instructions", "L2", project=project) == (
        "L2 текст".encode()
    )
    assert await store.read("instructions", "L3", project=project) == (
        "L3 текст".encode()
    )
    listed = await store.list("skill", "project", project=project)
    assert [e.name for e in listed] == ["s"]
    assert store.label("instructions", "L3", project=project) == str(
        root / "AGENTS.md"
    )
    with pytest.raises(ValueError, match="проект"):
        await store.read("instructions", "L3")
    with pytest.raises(ValueError, match="проект"):
        await store.list("skill", "project")


async def test_run_context_gives_its_project_to_the_skills_registry(
    tmp_path, monkeypatch
):
    """Реестр скиллов своего хранилища строится с проектом рана (слой
    ``project``), до появления контекста; id — ``agent.project_id``."""
    import os

    from gc_core.run_context import acreate_run_context
    from gc_testkit.memory_agent_store import MemoryAgentStore

    seen = []

    class Recording(MemoryAgentStore):
        def skills_registry(self, settings, *, backend, project=None):
            seen.append(project)
            return super().skills_registry(
                settings, backend=backend, project=project
            )

    monkeypatch.chdir(tmp_path)
    cfg = make_settings(
        {
            "agent.model": "M",
            "agent.project_id": "p1",
            "logging.save_logs": False,
        }
    )
    await acreate_run_context(cfg, agent_store=Recording())
    assert seen and seen[0] == ProjectKey("p1", os.getcwd())
