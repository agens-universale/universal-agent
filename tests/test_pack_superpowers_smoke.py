"""Smoke tests for the bundled superpowers pack content (new geometry).

Verifies:
- pack.yaml exists and has the expected manifest fields
- all 10 SKILL.md files are present with valid frontmatter
- the using-superpowers (bootstrap) skill mentions all other 9 canonical pack ids
- all reference files exist
- the registry discovers all 10 skills, filters them per the new API
- bootstrap entry has is_pack_bootstrap=true and pack_size=10
"""

from pathlib import Path

import pytest
import yaml


PACK_DIR = Path(__file__).resolve().parents[1] / "skills" / "superpowers"

EXPECTED_SKILL_IDS = {
    "using-superpowers",
    "brainstorming",
    "writing-plans",
    "executing-plans",
    "test-driven-development",
    "systematic-debugging",
    "verification-before-completion",
    "requesting-code-review",
    "receiving-code-review",
    "writing-skills",
}

EXPECTED_REFERENCES = {
    "test-driven-development": {"testing-anti-patterns"},
    "systematic-debugging": {"root-cause-tracing", "defense-in-depth", "condition-based-waiting"},
}


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
    assert m["id"] == "superpowers"
    assert m["bootstrap_skill"] == "using-superpowers"
    assert isinstance(m["name"], str) and m["name"].strip()
    assert isinstance(m["description"], str) and m["description"].strip()
    assert isinstance(m["version"], str) and m["version"].strip()


def test_pack_has_readme():
    readme = PACK_DIR / "README.md"
    assert readme.is_file()
    body = readme.read_text(encoding="utf-8")
    assert "Superpowers" in body
    assert "skills_load" in body
    assert "using-superpowers" in body


@pytest.mark.parametrize("skill_id", sorted(EXPECTED_SKILL_IDS))
def test_skill_md_present_with_valid_frontmatter(skill_id):
    skill_md = PACK_DIR / skill_id / "SKILL.md"
    assert skill_md.is_file(), f"{skill_md} missing"
    fm = _parse_frontmatter(skill_md)
    assert fm["id"] == skill_id
    assert isinstance(fm["name"], str) and fm["name"].strip()
    assert isinstance(fm["description"], str) and fm["description"].strip()


def test_using_superpowers_mentions_all_other_skill_ids():
    skill_md = PACK_DIR / "using-superpowers" / "SKILL.md"
    body = skill_md.read_text(encoding="utf-8")
    other_skills = EXPECTED_SKILL_IDS - {"using-superpowers"}
    missing = [sid for sid in other_skills if f"superpowers:{sid}" not in body]
    assert not missing, f"using-superpowers body missing references to: {missing}"


@pytest.mark.parametrize(
    "skill_id, slug",
    sorted(
        (skill_id, slug)
        for skill_id, slugs in EXPECTED_REFERENCES.items()
        for slug in slugs
    ),
)
def test_reference_file_present(skill_id, slug):
    ref = PACK_DIR / skill_id / "references" / f"{slug}.md"
    assert ref.is_file()
    body = ref.read_text(encoding="utf-8")
    assert len(body.strip()) > 100


def test_registry_default_list_returns_only_bootstrap(tmp_path):
    """default list_skills() возвращает bootstrap superpowers + flat-скиллы. Internal скрыты."""
    from tools.skills.registry import SkillsRegistry

    repo_skills = PACK_DIR.parent  # <repo>/skills
    reg = SkillsRegistry(
        system_root=str(repo_skills),
        user_root=str(tmp_path / "u_empty"),
        project_root=str(tmp_path / "p_empty"),
    )
    items = {i["id"]: i for i in reg.list_skills() if i.get("pack_id") == "superpowers"}
    # В default списке только bootstrap
    assert set(items.keys()) == {"superpowers:using-superpowers"}
    bootstrap = items["superpowers:using-superpowers"]
    assert bootstrap["is_pack_bootstrap"] is True
    assert bootstrap["pack_size"] == 10


def test_registry_pack_param_returns_all_10_internals(tmp_path):
    """list_skills(pack='superpowers') возвращает все 10 internal."""
    from tools.skills.registry import SkillsRegistry

    repo_skills = PACK_DIR.parent
    reg = SkillsRegistry(
        system_root=str(repo_skills),
        user_root=str(tmp_path / "u_empty"),
        project_root=str(tmp_path / "p_empty"),
    )
    items = reg.list_skills(pack="superpowers")
    canonical_ids = {f"superpowers:{sid}" for sid in EXPECTED_SKILL_IDS}
    assert {i["id"] for i in items} == canonical_ids
