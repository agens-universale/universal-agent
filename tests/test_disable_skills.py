"""
Tests for `agent.disable_skills` filtering in SkillsRegistry.

Covers four cases (spec section 7, step 7):
1. Empty list → all skills visible (control).
2. Disable a flat skill id → it is gone; load_skill raises KeyError.
3. Disable a pack id → bootstrap AND inner skills are all gone.
4. Project overlay extends the list (concat+dedup via deep_merge).
"""

import pytest

from config_modules.merge import deep_merge
from tools.skills.registry import SkillsRegistry, get_skills_registry


def test_disable_skills_empty_list_keeps_all_skills() -> None:
    reg_default = SkillsRegistry()
    reg_empty = SkillsRegistry(disable_skills=[])
    assert set(reg_default.skill_catalog().canonical_ids) == set(
        reg_empty.skill_catalog().canonical_ids
    )


def test_disable_flat_skill_removes_it() -> None:
    flat_id = "computer-use"
    baseline = SkillsRegistry()
    # Sanity: skill exists in baseline (otherwise the test is trivially true).
    assert flat_id in baseline.skill_catalog().canonical_ids

    reg = SkillsRegistry(disable_skills=[flat_id])
    cat = reg.skill_catalog()
    assert flat_id not in cat.canonical_ids
    # Other skills remain visible.
    assert "topic-research" in cat.canonical_ids

    with pytest.raises(KeyError):
        reg.load_skill_detail(flat_id)


def test_disable_pack_removes_bootstrap_and_inner() -> None:
    pack_id = "articles"
    baseline = SkillsRegistry()
    cat_baseline = baseline.skill_catalog()
    # Sanity: pack and at least one inner are in baseline.
    assert cat_baseline.pack_bootstrap.get(pack_id) == "articles:using-articles"
    assert "articles:article-creator" in cat_baseline.canonical_ids
    assert "articles:check-article" in cat_baseline.canonical_ids

    reg = SkillsRegistry(disable_skills=[pack_id])
    cat = reg.skill_catalog()
    assert pack_id not in cat.pack_bootstrap
    assert "articles:using-articles" not in cat.canonical_ids
    assert "articles:article-creator" not in cat.canonical_ids
    assert "articles:check-article" not in cat.canonical_ids
    # Unrelated skills remain visible.
    assert "topic-research" in cat.canonical_ids

    with pytest.raises(KeyError):
        reg.load_skill_detail("articles:article-creator")


def test_disable_skills_overlay_concat_and_dedup() -> None:
    base = {"agent": {"disable_skills": ["computer-use"]}}
    overlay = {"agent": {"disable_skills": ["articles", "computer-use"]}}
    merged = deep_merge(base, overlay)
    # concat + dedup, preserving order
    assert merged["agent"]["disable_skills"] == ["computer-use", "articles"]


def test_get_skills_registry_reads_disable_from_config_dict() -> None:
    config_dict = {"disable_skills": ["computer-use"]}
    reg = get_skills_registry(config_dict)
    cat = reg.skill_catalog()
    assert "computer-use" not in cat.canonical_ids
    assert "topic-research" in cat.canonical_ids


def test_get_skills_registry_reads_pack_disable_from_config_dict() -> None:
    config_dict = {"disable_skills": ["science-article-creator"]}
    reg = get_skills_registry(config_dict)
    cat = reg.skill_catalog()
    # The new science pack must not be visible when listed in disable_skills.
    assert "science-article-creator" not in cat.pack_bootstrap
    assert "science-article-creator:scientific-research" not in cat.canonical_ids
    assert "science-article-creator:using-science-article-creator" not in cat.canonical_ids
