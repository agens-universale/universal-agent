"""skill_view — RU-метки типа, title/body вьювера."""

from gc_core.tools.skills.skill_arg_resolver import catalog_from_entries

from gc_tui.services.skill_view import (
    build_skill_viewer,
    skill_input_name,
    source_label,
    truncate_path_left,
)


def test_source_label_ru():
    assert source_label("system") == "системный"
    assert source_label("user") == "пользовательский"
    assert source_label("project") == "проектный"
    assert source_label("???") == "???"  # неизвестный — как есть


def test_build_skill_viewer_title_and_body():
    detail = {
        "id": "topic-research",
        "name": "Topic Research",
        "source": "system",
        "root_path": "/abs/skills/topic-research",
        "description": "desc",
        "instructions": "do things",
    }
    title, body = build_skill_viewer(detail)
    assert (
        title
        == "skill · topic-research · системный · /abs/skills/topic-research"
    )
    assert "# Topic Research (topic-research)" in body
    assert "desc" in body
    assert "do things" in body


def test_build_skill_viewer_handles_empty_fields():
    detail = {"id": "x", "source": "user", "root_path": "/p/x"}
    title, body = build_skill_viewer(detail)
    assert title == "skill · x · пользовательский · /p/x"
    assert "(без описания)" in body
    assert "(нет инструкций)" in body


def test_truncate_path_left_short_path_unchanged():
    assert truncate_path_left("/abs/skills/x", 40) == "/abs/skills/x"


def test_truncate_path_left_keeps_tail_with_ellipsis():
    p = "/very/long/path/skills/articles/article-creator"
    out = truncate_path_left(p, 20)
    assert len(out) == 20
    assert out.startswith("…")
    assert out.endswith("article-creator")


def test_truncate_path_left_exact_length_unchanged():
    assert truncate_path_left("abc", 3) == "abc"


_CATALOG = catalog_from_entries(
    [
        {"id": "supercoder:brainstorming"},
        {"id": "a:dup"},
        {"id": "b:dup"},
        {"id": "x:review"},
        {"id": "solo"},
    ]
)


def test_skill_input_name_short_when_unambiguous():
    name = skill_input_name("supercoder:brainstorming", _CATALOG)
    assert name == "brainstorming"


def test_skill_input_name_full_id_when_short_is_ambiguous():
    assert skill_input_name("a:dup", _CATALOG) == "a:dup"


def test_skill_input_name_full_id_when_short_is_a_command():
    name = skill_input_name(
        "x:review", _CATALOG, is_command=lambda n: n == "review"
    )
    assert name == "x:review"


def test_skill_input_name_without_pack_is_id():
    assert skill_input_name("solo", _CATALOG) == "solo"


def test_skill_input_name_without_catalog_is_short_name():
    assert skill_input_name("a:dup", None) == "dup"
