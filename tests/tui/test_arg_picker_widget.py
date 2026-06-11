"""ArgPicker — чистая логика фильтра/навигации (без mount/app)."""
import pytest


_OPTS = [("txt", "вся сессия в читаемом виде"), ("json", "структурированные данные сессии")]


def test_empty_filter_returns_all():
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    p = ArgPicker(_OPTS, initial_filter="")
    assert [o[0] for o in p._visible_rows()] == ["txt", "json"]


def test_prefix_filter_matches_value():
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    p = ArgPicker(_OPTS, initial_filter="j")
    assert [o[0] for o in p._visible_rows()] == ["json"]
    assert p.selected_value() == "json"


def test_filter_case_insensitive():
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    p = ArgPicker(_OPTS, initial_filter="TX")
    assert [o[0] for o in p._visible_rows()] == ["txt"]


def test_no_match_returns_empty():
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    p = ArgPicker(_OPTS, initial_filter="zzz")
    assert p._visible_rows() == []
    assert p.selected_value() is None


def test_move_selection_wraps_around():
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    p = ArgPicker(_OPTS, initial_filter="")
    assert p.selected_value() == "txt"
    p.move_selection(1)
    assert p.selected_value() == "json"
    p.move_selection(1)
    assert p.selected_value() == "txt"


def test_set_filter_resets_selection_to_zero():
    pytest.importorskip("textual")
    from tui.widgets.arg_picker import ArgPicker

    p = ArgPicker(_OPTS, initial_filter="")
    p.move_selection(1)
    assert p.selected_value() == "json"
    p.set_filter("")
    assert p.selected_value() == "txt"
