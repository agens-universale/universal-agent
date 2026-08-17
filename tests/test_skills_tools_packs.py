"""Tests for SkillsListTool and other tool wrappers with packs (new geometry).

Tests themselves are added in Task 9; this file currently contains only the
fixture to keep TDD ordering clean."""

import pytest


@pytest.fixture
def reg_with_packs(tmp_path):
    """Registry with two packs that share an inner skill name (for ambiguity tests)."""
    import yaml
    from gc_core.tools.skills.registry import SkillsRegistry

    system = tmp_path / "system"
    system.mkdir()
    # Pack "alpha" with inner skills "shared-name" and "unique-name"
    for inner in ("shared-name", "unique-name"):
        (system / "alpha" / inner).mkdir(parents=True)
        (system / "alpha" / inner / "SKILL.md").write_text(
            f"---\nid: {inner}\nname: {inner}\ndescription: \"\"\n---\n# body\n",
            encoding="utf-8",
        )
    (system / "alpha" / "pack.yaml").write_text(
        yaml.safe_dump({"id": "alpha", "bootstrap_skill": "unique-name"}),
        encoding="utf-8",
    )
    # Pack "beta" with inner skill "shared-name"
    (system / "beta" / "shared-name").mkdir(parents=True)
    (system / "beta" / "shared-name" / "SKILL.md").write_text(
        "---\nid: shared-name\nname: shared-name\ndescription: \"\"\n---\n# body\n",
        encoding="utf-8",
    )
    (system / "beta" / "pack.yaml").write_text(
        yaml.safe_dump({"id": "beta", "bootstrap_skill": "shared-name"}),
        encoding="utf-8",
    )
    return SkillsRegistry(
        system_root=str(system),
        user_root=str(tmp_path / "u_empty"),
        project_root=str(tmp_path / "p_empty"),
    )


def test_skills_list_default_hides_pack_internals(reg_with_packs):
    """Default skills_list: только bootstrap-скиллы пакетов и flat. Internal скрыты."""
    import json
    from gc_core.tools.skills.skills_tools import SkillsListTool
    tool = SkillsListTool(reg_with_packs)
    payload = json.loads(tool())
    assert payload["ok"] is True
    ids = {s["id"] for s in payload["result"]["skills"]}
    # alpha's bootstrap is unique-name, beta's bootstrap is shared-name
    assert ids == {"alpha:unique-name", "beta:shared-name"}


def test_skills_list_with_pack_returns_internals(reg_with_packs):
    """skills_list(pack='alpha') возвращает все internal alpha-пакета."""
    import json
    from gc_core.tools.skills.skills_tools import SkillsListTool
    tool = SkillsListTool(reg_with_packs)
    payload = json.loads(tool(pack="alpha"))
    assert payload["ok"] is True
    ids = {s["id"] for s in payload["result"]["skills"]}
    assert ids == {"alpha:shared-name", "alpha:unique-name"}


def test_skills_list_payload_has_is_pack_bootstrap_field(reg_with_packs):
    """Все entries имеют is_pack_bootstrap (true для bootstrap, false иначе)."""
    import json
    from gc_core.tools.skills.skills_tools import SkillsListTool
    tool = SkillsListTool(reg_with_packs)
    payload = json.loads(tool())
    by_id = {s["id"]: s for s in payload["result"]["skills"]}
    assert by_id["alpha:unique-name"]["is_pack_bootstrap"] is True
    assert by_id["beta:shared-name"]["is_pack_bootstrap"] is True
    assert by_id["alpha:unique-name"]["pack_size"] == 2
    assert by_id["beta:shared-name"]["pack_size"] == 1


def test_skills_list_internal_skill_payload_fields(reg_with_packs):
    """Internal non-bootstrap скиллы: is_pack_bootstrap=false, нет pack_size."""
    import json
    from gc_core.tools.skills.skills_tools import SkillsListTool
    tool = SkillsListTool(reg_with_packs)
    payload = json.loads(tool(pack="alpha"))
    by_id = {s["id"]: s for s in payload["result"]["skills"]}
    inner = by_id["alpha:shared-name"]
    assert inner["is_pack_bootstrap"] is False
    assert "pack_size" not in inner


def test_skills_load_ambiguous_returns_envelope_error(reg_with_packs):
    """skills_load("shared-name") → AMBIGUOUS_SKILL_ID envelope (alpha + beta оба имеют)."""
    import json
    from gc_core.tools.skills.skills_tools import LoadSkillTool
    tool = LoadSkillTool(reg_with_packs)
    payload = json.loads(tool(skill_id="shared-name"))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AMBIGUOUS_SKILL_ID"
    assert set(payload["error"]["candidates"]) == {"alpha:shared-name", "beta:shared-name"}


def test_skills_load_reference_ambiguous_returns_envelope_error(reg_with_packs):
    import json
    from gc_core.tools.skills.skills_tools import LoadReferenceTool
    tool = LoadReferenceTool(reg_with_packs)
    payload = json.loads(tool(skill_id="shared-name", slug="anything"))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AMBIGUOUS_SKILL_ID"


def test_skills_load_asset_ambiguous_returns_envelope_error(reg_with_packs):
    import json
    from gc_core.tools.skills.skills_tools import LoadAssetTool
    tool = LoadAssetTool(reg_with_packs)
    payload = json.loads(tool(skill_id="shared-name", slug="anything"))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AMBIGUOUS_SKILL_ID"


def test_run_skill_script_ambiguous_returns_envelope_error(reg_with_packs):
    import json
    from gc_core.tools.skills.skills_tools import RunSkillScriptTool
    tool = RunSkillScriptTool(reg_with_packs)
    payload = json.loads(tool(skill_id="shared-name"))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AMBIGUOUS_SKILL_ID"
