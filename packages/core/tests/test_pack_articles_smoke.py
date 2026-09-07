from gc_core.tools.skills.skill_arg_resolver import resolve_skill_arg, Resolved
from gc_core.tools.skills.skills_policy import render_skills_catalog
from gc_core.tools.skills.registry import SkillsRegistry


def test_articles_pack_bootstrap_mapped():
    cat = SkillsRegistry().skill_catalog()
    assert cat.pack_bootstrap.get("articles") == "articles:using-articles"
    assert "articles:article-creator" in cat.canonical_ids
    assert "articles:check-article" in cat.canonical_ids
    # the pack folder name itself is NOT a canonical skill id
    assert "articles" not in cat.canonical_ids


def test_articles_pack_default_list_hides_inner_shows_bootstrap():
    entries = SkillsRegistry().list_skills()
    by_id = {e["id"]: e for e in entries}
    assert "articles:using-articles" in by_id
    bootstrap = by_id["articles:using-articles"]
    assert bootstrap["is_pack_bootstrap"] is True
    assert bootstrap["pack_size"] == 3
    # inner canonical ids are hidden from the default flat menu
    assert "articles:article-creator" not in by_id
    assert "articles:check-article" not in by_id
    # and the old flat ids no longer exist
    assert "article-creator" not in by_id
    assert "check-article" not in by_id


def test_articles_pack_collapses_in_rendered_catalog():
    out = render_skills_catalog(SkillsRegistry().list_skills())
    assert "articles:using-articles" in out
    # the two inner skills are fully hidden — their canonical ids appear nowhere
    # (use exact canonical-id substrings, not bare names, because other packs may
    # legitimately have similar-looking names, e.g. `science-article-creator`).
    assert "articles:article-creator" not in out
    assert "articles:check-article" not in out


def test_articles_inner_skills_still_loadable():
    reg = SkillsRegistry()
    for cid in ("articles:article-creator", "articles:check-article"):
        detail = reg.load_skill_detail(cid)
        assert detail["id"] == cid


def test_articles_pack_resolves_via_cli_s():
    cat = SkillsRegistry().skill_catalog()
    r = resolve_skill_arg("articles", cat)
    assert isinstance(r, Resolved)
    assert r.canonical_id == "articles:using-articles"


def test_articles_inner_skills_resolve_by_bare_tail():
    cat = SkillsRegistry().skill_catalog()
    for bare, expected in (
        ("article-creator", "articles:article-creator"),
        ("check-article", "articles:check-article"),
    ):
        r = resolve_skill_arg(bare, cat)
        assert isinstance(r, Resolved)
        assert r.canonical_id == expected
