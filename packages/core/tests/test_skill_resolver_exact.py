from gc_core.tools.skills.skill_arg_resolver import (
    Failed,
    Resolved,
    catalog_from_entries,
    resolve_skill_exact,
    suggest_skills,
)

ENTRIES = [
    {"id": "supercoder", "pack_id": None, "is_pack_bootstrap": False},
    {"id": "codebase:entry", "pack_id": "codebase", "is_pack_bootstrap": True},
    {"id": "codebase:review", "pack_id": "codebase"},
    {"id": "articles:review", "pack_id": "articles"},
]


def _cat():
    return catalog_from_entries(ENTRIES)


def test_catalog_from_entries_shape():
    cat = _cat()
    assert cat.canonical_ids == {
        "supercoder",
        "codebase:entry",
        "codebase:review",
        "articles:review",
    }
    assert cat.bare_to_canonical["review"] == (
        "articles:review",
        "codebase:review",
    )
    assert cat.bare_to_canonical["supercoder"] == ("supercoder",)
    assert cat.pack_bootstrap == {"codebase": "codebase:entry"}


def test_exact_full_id():
    assert resolve_skill_exact("codebase:review", _cat()) == Resolved(
        "codebase:review"
    )


def test_exact_pack_to_bootstrap():
    res = resolve_skill_exact("codebase", _cat())
    assert isinstance(res, Resolved)
    assert res.canonical_id == "codebase:entry"


def test_exact_short_name():
    assert resolve_skill_exact("entry", _cat()) == Resolved("codebase:entry")
    assert resolve_skill_exact("supercoder", _cat()) == Resolved("supercoder")


def test_exact_ambiguous_short_name_fails_with_candidates():
    res = resolve_skill_exact("review", _cat())
    assert res == Failed("ambiguous", ["articles:review", "codebase:review"])


def test_exact_typo_fails_but_is_suggested():
    cat = _cat()
    assert resolve_skill_exact("suprcoder", cat) == Failed("not_found", [])
    assert "supercoder" in suggest_skills("suprcoder", cat)


def test_exact_empty():
    assert resolve_skill_exact("  ", _cat()) == Failed("empty", [])


def test_suggest_limits_and_no_match():
    cat = _cat()
    assert suggest_skills("zzzzzz", cat) == []
    assert len(suggest_skills("review", cat, n=1)) <= 1
