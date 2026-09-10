"""Smoke tests for the bundled default-coworker pack."""

from pathlib import Path

import pytest
import yaml
from gc_testkit.paths import ASSETS


PACK_DIR = ASSETS / "skills" / "default-coworker"

EXPECTED_SKILL_IDS = {
    "using-default-coworker",
    "research",
    "personal-assistant",
    "background-business",
    "deliverables-evidence",
    "handoff-autonomy",
}

INNER_SKILL_IDS = EXPECTED_SKILL_IDS - {"using-default-coworker"}


def _parse_frontmatter(skill_md: Path) -> dict:
    text = skill_md.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{skill_md} missing frontmatter opening"
    end = text.find("\n---\n", 4)
    assert end > 0, f"{skill_md} missing frontmatter closing"
    return yaml.safe_load(text[4:end])


def test_pack_manifest_present_and_valid():
    manifest_path = PACK_DIR / "pack.yaml"
    assert manifest_path.is_file()
    m = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert m["id"] == "default-coworker"
    assert m["bootstrap_skill"] == "using-default-coworker"
    assert isinstance(m["name"], str) and m["name"].strip()
    assert isinstance(m["description"], str) and m["description"].strip()
    assert isinstance(m["version"], str) and m["version"].strip()


def test_pack_has_readme():
    readme = PACK_DIR / "README.md"
    assert readme.is_file()
    body = readme.read_text(encoding="utf-8")
    assert "Default Coworker" in body
    assert "using-default-coworker" in body


@pytest.mark.parametrize("skill_id", sorted(EXPECTED_SKILL_IDS))
def test_skill_md_present_with_valid_frontmatter(skill_id):
    skill_md = PACK_DIR / skill_id / "SKILL.md"
    assert skill_md.is_file(), f"{skill_md} missing"
    fm = _parse_frontmatter(skill_md)
    assert fm["id"] == skill_id
    assert isinstance(fm["name"], str) and fm["name"].strip()
    assert isinstance(fm["description"], str) and fm["description"].strip()


def test_bootstrap_mentions_all_inner_skill_ids():
    body = (PACK_DIR / "using-default-coworker" / "SKILL.md").read_text(encoding="utf-8")
    missing = [
        sid for sid in INNER_SKILL_IDS if f"default-coworker:{sid}" not in body
    ]
    assert not missing, f"bootstrap missing inner routes: {missing}"


@pytest.mark.parametrize("skill_id", sorted(INNER_SKILL_IDS))
def test_inner_skills_do_not_reference_sibling_inners(skill_id):
    """Low coupling: only bootstrap routes between coworker inner skills."""
    body = (PACK_DIR / skill_id / "SKILL.md").read_text(encoding="utf-8")
    siblings = INNER_SKILL_IDS - {skill_id}
    offenders = [
        other for other in siblings if f"default-coworker:{other}" in body
    ]
    assert not offenders, (
        f"{skill_id} references sibling inner skill(s): {offenders}"
    )


async def test_registry_default_list_returns_only_bootstrap(tmp_path):
    from gc_core.tools.skills.registry import SkillsRegistry

    repo_skills = PACK_DIR.parent
    reg = SkillsRegistry(
        system_root=str(repo_skills),
        user_root=str(tmp_path / "u_empty"),
        project_root=str(tmp_path / "p_empty"),
    )
    items = {
        i["id"]: i for i in await reg.list_skills() if i.get("pack_id") == "default-coworker"
    }
    assert set(items.keys()) == {"default-coworker:using-default-coworker"}
    bootstrap = items["default-coworker:using-default-coworker"]
    assert bootstrap["is_pack_bootstrap"] is True
    assert bootstrap["pack_size"] == 6


async def test_registry_pack_param_returns_all_internals(tmp_path):
    from gc_core.tools.skills.registry import SkillsRegistry

    repo_skills = PACK_DIR.parent
    reg = SkillsRegistry(
        system_root=str(repo_skills),
        user_root=str(tmp_path / "u_empty"),
        project_root=str(tmp_path / "p_empty"),
    )
    items = await reg.list_skills(pack="default-coworker")
    canonical_ids = {f"default-coworker:{sid}" for sid in EXPECTED_SKILL_IDS}
    assert {i["id"] for i in items} == canonical_ids
