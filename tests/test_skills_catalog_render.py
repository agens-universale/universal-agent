from states import render_skills_catalog

B_ALPHA = {"id": "alpha:using-alpha", "name": "Using Alpha",
           "description": "Alpha entry.", "pack_id": "alpha",
           "is_pack_bootstrap": True, "pack_size": 4}
B_BETA = {"id": "beta:using-beta", "name": "Using Beta",
          "description": "Beta entry.", "pack_id": "beta",
          "is_pack_bootstrap": True, "pack_size": 7}
F_PO = {"id": "project-overview", "name": "po", "description": "Map a repo.",
        "pack_id": None, "is_pack_bootstrap": False}
F_TR = {"id": "topic-research", "name": "tr", "description": "Research a topic.",
        "pack_id": None, "is_pack_bootstrap": False}
B_NOSIZE = {"id": "gamma:using-gamma", "description": "Gamma entry.",
            "is_pack_bootstrap": True}

HEADER = "For solving the task you can use skills."
CORE = ('Pick a skill by fit to the OVERALL task, not the first sub-step '
        'you decompose it into')
PACK_CLAUSE = "weigh it as a whole-task candidate alongside standalone skills"
EXC = "Exception: trivial / read-only / nothing fits — just proceed."
PACKS_H = ("## Skill packs — load the bootstrap skill to enter a guided "
           "multi-step workflow")
STAND_H = "## Standalone skills"


def test_both_groups():
    out = render_skills_catalog([F_TR, B_BETA, F_PO, B_ALPHA])
    assert HEADER in out
    assert CORE in out
    assert PACK_CLAUSE in out
    assert EXC in out
    assert PACKS_H in out
    assert STAND_H in out
    assert out.index(PACKS_H) < out.index(STAND_H)
    assert out.index("alpha:using-alpha") < out.index("beta:using-beta")
    assert "(pack size: 4)" in out and "(pack size: 7)" in out
    assert out.index("project-overview") < out.index("topic-research")
    assert "- project-overview — Map a repo." in out
    assert "- alpha:using-alpha — Alpha entry. (pack size: 4)" in out


def test_only_flats_no_pack_section_no_pack_clause():
    out = render_skills_catalog([F_PO, F_TR])
    assert HEADER in out
    assert CORE in out
    assert EXC in out
    assert PACKS_H not in out
    assert PACK_CLAUSE not in out
    assert STAND_H in out
    assert "- topic-research — Research a topic." in out


def test_only_bootstraps_no_standalone_section():
    out = render_skills_catalog([B_ALPHA])
    assert PACKS_H in out
    assert PACK_CLAUSE in out
    assert STAND_H not in out
    assert "- alpha:using-alpha — Alpha entry. (pack size: 4)" in out


def test_deterministic_regardless_of_input_order():
    a = render_skills_catalog([F_TR, B_BETA, F_PO, B_ALPHA])
    b = render_skills_catalog([B_ALPHA, F_PO, B_BETA, F_TR])
    assert a == b


def test_bootstrap_without_pack_size_does_not_crash():
    out = render_skills_catalog([B_NOSIZE])
    assert "- gamma:using-gamma — Gamma entry." in out
    assert "(pack size:" not in out


def test_empty_list_header_and_core_only():
    out = render_skills_catalog([])
    assert HEADER in out
    assert CORE in out
    assert PACKS_H not in out
    assert STAND_H not in out
    assert PACK_CLAUSE not in out


def test_entries_without_id_do_not_crash():
    # The old json.dumps tolerated arbitrary skill dicts; the renderer must
    # too (an existing State-roundtrip test feeds id-less skill dicts).
    out = render_skills_catalog([{"name": "skill_a", "version": 1},
                                 {"name": "skill_b", "version": 2}])
    assert HEADER in out
    assert CORE in out
    assert STAND_H in out


def test_only_id_and_description_surfaced():
    out = render_skills_catalog([
        {"id": "p:b", "name": "ZZNAME", "description": "PackDesc.",
         "source": "ZZSOURCE", "pack_id": "ZZPACK", "is_pack_bootstrap": True,
         "pack_size": 2},
        {"id": "flat", "name": "YYNAME", "description": "FlatDesc.",
         "source": "YYSOURCE", "pack_id": None, "is_pack_bootstrap": False},
    ])
    assert "p:b" in out and "PackDesc." in out
    assert "flat" in out and "FlatDesc." in out
    assert "ZZNAME" not in out and "ZZSOURCE" not in out and "ZZPACK" not in out
    assert "YYNAME" not in out and "YYSOURCE" not in out
