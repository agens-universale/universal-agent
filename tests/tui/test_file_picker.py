"""FilePicker — overlay список путей, set_entries (внешний листинг), навигация."""
from tui.widgets.file_picker import FilePicker


def test_selected_entry_default_first():
    p = FilePicker(["agents/", "a.py", "b.py"])
    assert p.selected_entry() == "agents/"


def test_move_selection_wraps():
    p = FilePicker(["a", "b", "c"])
    p.move_selection(1)
    assert p.selected_entry() == "b"
    p.move_selection(-1)
    p.move_selection(-1)
    assert p.selected_entry() == "c"  # wrap


def test_set_entries_resets_selection():
    p = FilePicker(["a", "b"])
    p.move_selection(1)
    p.set_entries(["x", "y", "z"])
    assert p.selected_entry() == "x"


def test_empty_entries_selected_none():
    p = FilePicker([])
    assert p.selected_entry() is None
