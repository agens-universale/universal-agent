from tools.skills.registry import Skill


def test_skill_has_source_field():
    s = Skill(
        id="x",
        name="x",
        description="",
        version=None,
        root_path="/tmp/x",
        instructions="",
        has_scripts=False,
        source="system",
    )
    assert s.source == "system"


from pathlib import Path

import pytest

from tools.skills.registry import SkillsRegistry


def _write_skill(root: Path, skill_id: str, description: str) -> None:
    skill_dir = root / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nid: {skill_id}\nname: {skill_id}\ndescription: \"{description}\"\n---\n# body of {skill_id}\n",
        encoding="utf-8",
    )


@pytest.fixture
def three_layer_registry(tmp_path):
    system = tmp_path / "system"
    user = tmp_path / "user"
    project = tmp_path / "project"
    for d in (system, user, project):
        d.mkdir()
    reg = SkillsRegistry(
        system_root=str(system),
        user_root=str(user),
        project_root=str(project),
    )
    return reg, system, user, project


def test_only_system_skills(three_layer_registry):
    reg, system, _, _ = three_layer_registry
    _write_skill(system, "alpha", "from system")
    _write_skill(system, "beta", "from system")
    items = reg.list_skills()
    ids = {i["id"]: i for i in items}
    assert set(ids) == {"alpha", "beta"}
    assert all(i["source"] == "system" for i in items)


def test_user_overrides_system(three_layer_registry):
    reg, system, user, _ = three_layer_registry
    _write_skill(system, "alpha", "from system")
    _write_skill(user, "alpha", "from user")
    items = reg.list_skills()
    assert len(items) == 1
    assert items[0]["source"] == "user"
    assert items[0]["description"] == "from user"


def test_project_overrides_user_and_system(three_layer_registry):
    reg, system, user, project = three_layer_registry
    _write_skill(system, "alpha", "from system")
    _write_skill(user, "alpha", "from user")
    _write_skill(project, "alpha", "from project")
    items = reg.list_skills()
    assert len(items) == 1
    assert items[0]["source"] == "project"
    assert items[0]["description"] == "from project"


def test_three_layers_no_collision(three_layer_registry):
    reg, system, user, project = three_layer_registry
    _write_skill(system, "a_system", "")
    _write_skill(user, "b_user", "")
    _write_skill(project, "c_project", "")
    items = {i["id"]: i["source"] for i in reg.list_skills()}
    assert items == {
        "a_system": "system",
        "b_user": "user",
        "c_project": "project",
    }


def test_missing_layer_skipped(tmp_path):
    system = tmp_path / "system"
    system.mkdir()
    _write_skill(system, "x", "")
    reg = SkillsRegistry(
        system_root=str(system),
        user_root=str(tmp_path / "nonexistent_user"),
        project_root=str(tmp_path / "nonexistent_project"),
    )
    items = reg.list_skills()
    assert len(items) == 1
    assert items[0]["id"] == "x"


def test_all_layers_empty(tmp_path):
    reg = SkillsRegistry(
        system_root=str(tmp_path / "no_system"),
        user_root=str(tmp_path / "no_user"),
        project_root=str(tmp_path / "no_project"),
    )
    assert reg.list_skills() == []


def test_load_skill_detail_returns_source(three_layer_registry):
    reg, system, user, _ = three_layer_registry
    _write_skill(system, "alpha", "from system")
    _write_skill(user, "alpha", "from user")
    detail = reg.load_skill_detail("alpha")
    assert detail["source"] == "user"
    assert "from user" in detail["instructions"] + detail["description"]


def test_list_skills_includes_root_path(three_layer_registry):
    reg, system, _, _ = three_layer_registry
    _write_skill(system, "alpha", "from system")
    items = reg.list_skills()
    assert items[0]["root_path"] == str(system / "alpha")


def test_load_skill_detail_includes_root_path(three_layer_registry):
    reg, system, _, _ = three_layer_registry
    _write_skill(system, "alpha", "from system")
    detail = reg.load_skill_detail("alpha")
    assert detail["root_path"] == str(system / "alpha")
