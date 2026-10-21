"""SkillPicker — чистая логика фильтра/навигации (без mount/app)."""

import pytest

_SKILLS = [  # порядок показа: без пакета, затем пакеты, всё по алфавиту
    {"id": "skill-creator"},
    {"id": "codebase:using-codebase"},
    {"id": "supercoder:coder"},
    {"id": "supercoder:critic"},
    {"id": "supercoder:planner"},
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
        "supercoder:coder",
        "supercoder:critic",
        "supercoder:planner",
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
    assert p.selected_skill()["id"] == "skill-creator"
    p.move_selection(1)
    assert p.selected_skill()["id"] == "codebase:using-codebase"
    p.move_selection(-2)
    # wrap: 1 -> -1 -> last index (len(_SKILLS) - 1)
    assert p.selected_skill()["id"] == _SKILLS[-1]["id"]


def test_set_filter_resets_selection_to_zero():
    pytest.importorskip("textual")
    from gc_tui.widgets.skill_picker import SkillPicker

    p = SkillPicker(_SKILLS, initial_filter="")
    p.move_selection(2)
    assert p.selected_skill()["id"] == "supercoder:coder"
    p.set_filter("code")
    # фильтр сменился: selected должен сброситься на индекс 0 текущей выборки
    assert p.selected_skill()["id"] == p._visible_rows()[0]["id"]


# --- группировка по пакетам (решение владельца после смоука P5) ---

_PACKED = [
    {"id": "articles:article-creator", "description": "пишет статьи"},
    {"id": "skill-creator", "description": "создаёт скиллы"},
    {"id": "default-coworker:research", "description": "исследование"},
    {"id": "articles:editor", "description": "правит"},
]


def _rows(picker):
    widgets, _ = picker._row_widgets()
    return widgets


def _plain(widget):
    return str(widget.content)


def test_flat_skills_first_without_header_then_pack_groups():
    pytest.importorskip("textual")
    from gc_tui.widgets.skill_picker import SkillPicker

    p = SkillPicker(_PACKED)
    rows = _rows(p)
    heads = [
        _plain(w).strip() for w in rows if w.has_class("palette-pack-head")
    ]
    assert heads == ["articles", "default-coworker"]
    assert not rows[0].has_class("palette-pack-head")
    assert [s["id"] for s in p._visible_rows()] == [
        "skill-creator",
        "articles:article-creator",
        "articles:editor",
        "default-coworker:research",
    ]
    assert not any(
        w.has_class("palette-row")
        for w in rows
        if w.has_class("palette-pack-head")
    )


def test_flat_skill_name_at_header_x_description_column_shared():
    pytest.importorskip("textual")
    from gc_tui.widgets.skill_picker import SkillPicker

    p = SkillPicker(_PACKED)
    rows = _rows(p)
    flat = rows[0].content.plain
    assert flat.startswith(" skill-creator ")
    head = _plain(rows[1])
    assert head.index("articles") == flat.index("skill-creator")


def test_skill_row_short_name_indented_with_grey_description():
    pytest.importorskip("textual")
    from gc_tui import palette as _pal
    from gc_tui.widgets.skill_picker import SkillPicker

    p = SkillPicker(_PACKED)
    row = _rows(p)[2]  # первая строка под шапкой articles
    assert row.has_class("palette-skill-row")
    text = row.content
    name_w = len("article-creator") + 3  # самое длинное видимое короткое имя
    assert (
        text.plain == "   " + "article-creator".ljust(name_w) + "пишет статьи"
    )
    assert "articles:" not in text.plain
    assert {str(s.style) for s in text.spans} == {_pal.DIM}
    assert text.spans[0].start == 3 + name_w


def test_descriptions_share_one_column():
    pytest.importorskip("textual")
    from gc_tui.widgets.skill_picker import SkillPicker

    p = SkillPicker(_PACKED)
    rows = [w for w in _rows(p) if w.has_class("palette-skill-row")]
    starts = {w.content.spans[0].start for w in rows}
    assert len(starts) == 1


def test_long_short_name_capped_at_32_with_ellipsis():
    pytest.importorskip("textual")
    from gc_tui.widgets.skill_picker import SkillPicker

    p = SkillPicker([{"id": "p:" + "x" * 40, "description": "d"}])
    row = _rows(p)[1]
    assert row.content.plain == "   " + "x" * 31 + "…" + "   d"


def test_description_cut_with_ellipsis_to_content_width():
    pytest.importorskip("textual")
    from gc_tui.widgets.skill_picker import SkillPicker

    p = SkillPicker([{"id": "p:ab", "description": "d" * 100}])
    p._content_width = lambda: 40
    plain = _rows(p)[1].content.plain
    assert len(plain) == 40 - 2  # padding строки
    assert plain.endswith("…")


def test_arrows_skip_pack_headers():
    pytest.importorskip("textual")
    from gc_tui.widgets.skill_picker import SkillPicker

    p = SkillPicker(_PACKED)
    seen = []
    for _ in range(len(_PACKED)):
        seen.append(p.selected_skill()["id"])
        p.move_selection(1)
    assert seen == [s["id"] for s in p._visible_rows()]
    p.move_selection(-1)
    assert p.selected_skill()["id"] == "default-coworker:research"
    _, selected = p._row_widgets()
    assert selected.has_class("palette-skill-row")
    assert not selected.has_class("palette-pack-head")


def test_filter_hides_groups_without_matches():
    pytest.importorskip("textual")
    from gc_tui.widgets.skill_picker import SkillPicker

    p = SkillPicker(_PACKED, initial_filter="research")
    heads = [
        _plain(w).strip() for w in _rows(p) if w.has_class("palette-pack-head")
    ]
    assert heads == ["default-coworker"]
    assert p.selected_skill()["id"] == "default-coworker:research"


def test_desired_height_counts_pack_headers():
    pytest.importorskip("textual")
    from gc_tui.widgets.skill_picker import SkillPicker

    p = SkillPicker(_PACKED)
    assert p.desired_height == len(_PACKED) + 2 + 2 + 2


_MIXED_LAYERS = [  # порядок реестра: системный слой, затем пользовательский
    {"id": "topic-research", "source": "system"},
    {"id": "zeta:beta", "source": "system"},
    {"id": "Complex-task", "source": "system"},
    {"id": "alpha:zed", "source": "system"},
    {"id": "alias-game", "source": "user"},
    {"id": "alpha:Able", "source": "user"},
    {"id": "zeta:alpha", "source": "user"},
]


def test_order_is_alphabetical_not_registry_layers():
    pytest.importorskip("textual")
    from gc_tui.widgets.skill_picker import SkillPicker

    p = SkillPicker(_MIXED_LAYERS)
    assert [s["id"] for s in p._visible_rows()] == [
        "alias-game",
        "Complex-task",
        "topic-research",
        "alpha:Able",
        "alpha:zed",
        "zeta:alpha",
        "zeta:beta",
    ]
    heads = [
        _plain(w).strip() for w in _rows(p) if w.has_class("palette-pack-head")
    ]
    assert heads == ["alpha", "zeta"]
