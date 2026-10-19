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
