from gc_core.tools.skills.registry import SkillsRegistry
from gc_core.tools.skills.skill_arg_resolver import Resolved, resolve_skill_arg
from gc_core.tools.skills.skills_policy import render_skills_catalog


async def test_codebase_pack_bootstrap_mapped():
    cat = await SkillsRegistry().skill_catalog()
    assert cat.pack_bootstrap.get("codebase") == "codebase:using-codebase"
    assert "codebase:project-overview" in cat.canonical_ids
    assert "codebase:project-investigation" in cat.canonical_ids
    # the pack folder name itself is NOT a canonical skill id
    assert "codebase" not in cat.canonical_ids


async def test_codebase_pack_default_list_hides_inner_shows_bootstrap():
    entries = await SkillsRegistry().list_skills()
    by_id = {e["id"]: e for e in entries}
    assert "codebase:using-codebase" in by_id
    bootstrap = by_id["codebase:using-codebase"]
    assert bootstrap["is_pack_bootstrap"] is True
    assert bootstrap["pack_size"] == 3
    # inner canonical ids are hidden from the default flat menu
    assert "codebase:project-overview" not in by_id
    assert "codebase:project-investigation" not in by_id
    # and the old flat ids no longer exist
    assert "project-overview" not in by_id
    assert "project-investigation" not in by_id


async def test_codebase_pack_collapses_in_rendered_catalog():
    out = render_skills_catalog(await SkillsRegistry().list_skills())
    assert "codebase:using-codebase" in out
    # the two inner skills are fully hidden — their ids appear nowhere in the catalog
    assert "project-overview" not in out
    assert "project-investigation" not in out


async def test_codebase_inner_skills_still_loadable():
    reg = SkillsRegistry()
    for cid in ("codebase:project-overview", "codebase:project-investigation"):
        detail = await reg.load_skill_detail(cid)
        assert detail["id"] == cid


async def test_codebase_pack_resolves_via_cli_s():
    cat = await SkillsRegistry().skill_catalog()
    r = resolve_skill_arg("codebase", cat)
    assert isinstance(r, Resolved)
    assert r.canonical_id == "codebase:using-codebase"
