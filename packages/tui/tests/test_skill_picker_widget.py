"""SkillPicker — чистая логика фильтра/навигации (без mount/app)."""

import pytest

_SKILLS = [
    {"id": "supercoder:planner"},
    {"id": "supercoder:coder"},
    {"id": "supercoder:critic"},
    {"id": "codebase:using-codebase"},
    {"id": "skill-creator"},
]


def test_empty_filter_returns_all():
    pytest.importorskip("textual")
    from gc_tui.widgets.skill_picker import SkillPicker

    p = SkillPicker(_SKILLS, initial_filter="")
    assert [s["id"] for s in p._visible_rows()] == [s["id"] for s in _SKILLS]


def test_substring_filter_matches_id():
    pytest.importorskip("textual")
    from gc_tui.widgets.skill_picker import SkillPicker

    p = SkillPicker(_SKILLS, initial_filter="super")
    ids = [s["id"] for s in p._visible_rows()]
    assert ids == [
        "supercoder:planner",
        "supercoder:coder",
        "supercoder:critic",
    ]


def test_filter_case_insensitive():
    pytest.importorskip("textual")
    from gc_tui.widgets.skill_picker import SkillPicker

    p = SkillPicker(_SKILLS, initial_filter="CODE")
    ids = [s["id"] for s in p._visible_rows()]
    assert "supercoder:coder" in ids
    assert "codebase:using-codebase" in ids


def test_filter_no_match_returns_empty():
    pytest.importorskip("textual")
    from gc_tui.widgets.skill_picker import SkillPicker

    p = SkillPicker(_SKILLS, initial_filter="zzz")
    assert p._visible_rows() == []
    assert p.selected_skill() is None


def test_move_selection_wraps_around():
    pytest.importorskip("textual")
    from gc_tui.widgets.skill_picker import SkillPicker

    p = SkillPicker(_SKILLS, initial_filter="")
    assert p.selected_skill()["id"] == "supercoder:planner"
    p.move_selection(1)
    assert p.selected_skill()["id"] == "supercoder:coder"
    p.move_selection(-2)
    # wrap: 1 -> -1 -> last index (len(_SKILLS) - 1)
    assert p.selected_skill()["id"] == _SKILLS[-1]["id"]


def test_set_filter_resets_selection_to_zero():
    pytest.importorskip("textual")
    from gc_tui.widgets.skill_picker import SkillPicker

    p = SkillPicker(_SKILLS, initial_filter="")
    p.move_selection(2)
    assert p.selected_skill()["id"] == "supercoder:critic"
    p.set_filter("code")
    # фильтр сменился: selected должен сброситься на индекс 0 текущей выборки
    assert p.selected_skill()["id"] == p._visible_rows()[0]["id"]
