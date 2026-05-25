import pytest

from tools.skills.skill_arg_resolver import (
    SkillCatalog,
    Resolved,
    Failed,
    resolve_skill_arg,
    FUZZY_CUTOFF,
)


def _catalog():
    # supercoder pack (bootstrap using-supercoder) + a flat skill;
    # 'dup' is an inner name present in two packs (ambiguous bare).
    return SkillCatalog(
        canonical_ids=frozenset({
            "supercoder:using-supercoder",
            "supercoder:brainstorming",
            "supercoder:dup",
            "otherpack:dup",
            "otherpack:using-otherpack",
            "project-overview",
        }),
        bare_to_canonical={
            "using-supercoder": ("supercoder:using-supercoder",),
            "brainstorming": ("supercoder:brainstorming",),
            "dup": ("otherpack:dup", "supercoder:dup"),
            "using-otherpack": ("otherpack:using-otherpack",),
            "project-overview": ("project-overview",),
        },
        pack_bootstrap={
            "supercoder": "supercoder:using-supercoder",
            "otherpack": "otherpack:using-otherpack",
        },
    )


def test_exact_canonical():
    assert resolve_skill_arg("supercoder:brainstorming", _catalog()) == Resolved(
        "supercoder:brainstorming", None
    )


def test_exact_pack_id_to_bootstrap():
    r = resolve_skill_arg("supercoder", _catalog())
    assert isinstance(r, Resolved)
    assert r.canonical_id == "supercoder:using-supercoder"
    assert r.note and "supercoder" in r.note


def test_bare_unique():
    assert resolve_skill_arg("brainstorming", _catalog()) == Resolved(
        "supercoder:brainstorming", None
    )


def test_flat_skill():
    assert resolve_skill_arg("project-overview", _catalog()) == Resolved(
        "project-overview", None
    )


def test_bare_ambiguous():
    r = resolve_skill_arg("dup", _catalog())
    assert r == Failed("ambiguous", ["otherpack:dup", "supercoder:dup"])


def test_empty():
    assert resolve_skill_arg("   ", _catalog()) == Failed("empty", [])


def test_underscore_normalization_canonical():
    assert resolve_skill_arg("supercoder:using_supercoder", _catalog()) == Resolved(
        "supercoder:using-supercoder", None
    )


def test_cutoff_constant():
    assert FUZZY_CUTOFF == 0.8


def test_typo_pack_to_bootstrap():
    r = resolve_skill_arg("suprcoder", _catalog())
    assert isinstance(r, Resolved)
    assert r.canonical_id == "supercoder:using-supercoder"
    assert r.note and "suprcoder" in r.note


def test_typo_skill():
    r = resolve_skill_arg("brainstrming", _catalog())
    assert isinstance(r, Resolved)
    assert r.canonical_id == "supercoder:brainstorming"
    assert r.note and "brainstrming" in r.note


def test_fuzzy_noise_not_found():
    r = resolve_skill_arg("zzzzzzzz", _catalog())
    assert r == Failed("not_found", [])


def test_fuzzy_lands_on_ambiguous_bare():
    # 'brainstrming' (~0.96 to 'brainstorming') fuzzy-closest to a bare name
    # that maps to two canonicals -> ambiguous. One short edit on a realistic
    # ~13-char id stays well above FUZZY_CUTOFF=0.8 (a 3-char fixture would
    # not: a single edit there is only ~0.67 ratio).
    cat = SkillCatalog(
        canonical_ids=frozenset({"packa:brainstorming", "packb:brainstorming"}),
        bare_to_canonical={
            "brainstorming": ("packa:brainstorming", "packb:brainstorming")
        },
        pack_bootstrap={},
    )
    r = resolve_skill_arg("brainstrming", cat)
    assert r == Failed("ambiguous", ["packa:brainstorming", "packb:brainstorming"])


def test_fuzzy_ambiguous_tie():
    # Input equidistant from two long, equally-similar pack ids (~0.92 each,
    # both above cutoff) -> refuse to guess.
    cat = SkillCatalog(
        canonical_ids=frozenset({"code-review-a:entry", "code-review-b:entry"}),
        bare_to_canonical={"entry": ("code-review-a:entry", "code-review-b:entry")},
        pack_bootstrap={
            "code-review-a": "code-review-a:entry",
            "code-review-b": "code-review-b:entry",
        },
    )
    r = resolve_skill_arg("code-review-z", cat)
    assert isinstance(r, Failed)
    assert r.reason == "fuzzy_ambiguous"
    assert {"code-review-a", "code-review-b"}.issubset(set(r.suggestions))


def test_precedence_pack_id_over_bare():
    # Exact name is both a pack id and a bare skill name -> pack id wins.
    cat = SkillCatalog(
        canonical_ids=frozenset({"shared:using-shared", "p:shared"}),
        bare_to_canonical={"using-shared": ("shared:using-shared",), "shared": ("p:shared",)},
        pack_bootstrap={"shared": "shared:using-shared"},
    )
    assert resolve_skill_arg("shared", cat) == Resolved(
        "shared:using-shared", note="pack 'shared' -> bootstrap 'shared:using-shared'"
    )
