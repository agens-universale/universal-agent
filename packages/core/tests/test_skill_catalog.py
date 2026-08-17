from gc_core.tools.skills.skill_arg_resolver import resolve_skill_arg, Resolved


def test_skill_catalog_shape_on_real_pack():
    # Import inside the test: test_skill_creator.py pops and reloads tools.*
    # modules; a module-level SkillCatalog would be a stale class object.
    from gc_core.tools.skills.skill_arg_resolver import SkillCatalog
    from gc_core.tools.skills.registry import SkillsRegistry

    cat = SkillsRegistry().skill_catalog()
    assert isinstance(cat, SkillCatalog)
    # supercoder pack present, bootstrap mapped
    assert cat.pack_bootstrap.get("supercoder") == "supercoder:using-supercoder"
    # internal canonical ids present
    assert "supercoder:brainstorming" in cat.canonical_ids
    # a flat skill present and bare-addressable
    assert "codebase:project-overview" in cat.canonical_ids
    assert cat.bare_to_canonical.get("project-overview") == ("codebase:project-overview",)
    # bare tail maps to canonical
    assert "supercoder:brainstorming" in cat.bare_to_canonical.get("brainstorming", ())
    # pack folder name itself is NOT a canonical skill id
    assert "supercoder" not in cat.canonical_ids
    # codebase pack present, bootstrap mapped, inner canonical, folder not a skill
    assert cat.pack_bootstrap.get("codebase") == "codebase:using-codebase"
    assert "codebase:project-investigation" in cat.canonical_ids
    assert "codebase" not in cat.canonical_ids

    # invariants the resolver relies on (content-independent)
    assert set(cat.pack_bootstrap.values()) <= cat.canonical_ids
    assert all(
        cid in cat.canonical_ids
        for ids in cat.bare_to_canonical.values()
        for cid in ids
    )


def test_resolver_outputs_are_loadable_against_real_registry():
    from gc_core.tools.skills.registry import SkillsRegistry

    reg = SkillsRegistry()
    cat = reg.skill_catalog()
    # "project-overview" is now a pack-inner skill: bare tail -> codebase:project-overview
    for raw in ["supercoder", "brainstorming", "supercoder:brainstorming",
                "suprcoder", "project-overview"]:
        r = resolve_skill_arg(raw, cat)
        assert isinstance(r, Resolved), f"{raw!r} -> {r}"
        # guard: every resolved id is actually loadable by the registry
        detail = reg.load_skill_detail(r.canonical_id)
        assert detail["id"] == r.canonical_id
