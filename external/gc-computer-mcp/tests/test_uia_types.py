from gc_computer_mcp.core.uia_types import UIAElement, UIASnapshot, render_tree_text


def make_element(id, name, ctype, parent=None, children=(), rid=None):
    return UIAElement(
        id=id, runtime_id=rid or f"rid-{id}", automation_id="", name=name,
        control_type=ctype, bounds=(0, 0, 10, 10), parent_id=parent,
        parent_path="/".join([]), children_ids=list(children),
    )


def test_render_tree_text_simple():
    root = make_element(0, "Desktop", "Pane", children=[1])
    child = make_element(1, "Excel", "Window", parent=0)
    snapshot = UIASnapshot(
        windows=[{"title": "Excel", "is_focused": True, "bounds": (0, 0, 1920, 1080)}],
        elements=[root, child],
        first_window_id=1,
    )
    text = render_tree_text(snapshot)
    assert "[0] Pane" in text
    assert "[1] Window" in text
    assert "Excel" in text


def test_render_tree_text_indent_by_depth():
    root = make_element(0, "", "Pane", children=[1])
    child = make_element(1, "x", "Button", parent=0)
    snap = UIASnapshot(windows=[], elements=[root, child], first_window_id=None)
    text = render_tree_text(snap)
    lines = text.splitlines()
    assert lines[0].startswith("[0]")
    assert lines[1].startswith("  [1]")


def test_snapshot_dict_roundtrip():
    from gc_computer_mcp.core.uia_types import snapshot_from_dict, snapshot_to_dict

    snap = UIASnapshot(
        windows=[{"id": 1, "title": "Excel", "bounds": [0, 0, 100, 100]}],
        elements=[
            UIAElement(0, "rid0", "aid0", "Desktop", "Pane",
                       (0, 0, 100, 100), None, "", [1]),
            UIAElement(1, "rid1", "aid1", "Excel", "Window",
                       (0, 0, 100, 100), 0, "Desktop", []),
        ],
        first_window_id=1,
        timestamp="2026-04-19T18:00:00+00:00",
    )

    restored = snapshot_from_dict(snapshot_to_dict(snap))
    assert restored.windows == snap.windows
    assert restored.first_window_id == snap.first_window_id
    assert restored.timestamp == snap.timestamp
    assert len(restored.elements) == len(snap.elements)
    for orig, r in zip(snap.elements, restored.elements, strict=True):
        assert r.id == orig.id
        assert r.runtime_id == orig.runtime_id
        assert r.name == orig.name
        assert r.control_type == orig.control_type
        assert tuple(r.bounds) == tuple(orig.bounds)


def test_render_tree_text_shows_value_for_edit_with_value():
    """EditControl with a non-empty value renders as: [N] Edit "Label" value="Foo"."""
    root = make_element(0, "", "Pane", children=[1])
    edit = UIAElement(
        id=1, runtime_id="rid-1", automation_id="", name="Ваше имя",
        control_type="Edit", bounds=(0, 0, 10, 10), parent_id=0,
        parent_path="", children_ids=[], value="Дмитрий",
    )
    snap = UIASnapshot(windows=[], elements=[root, edit], first_window_id=None)
    text = render_tree_text(snap)
    assert '[1] Edit "Ваше имя" value="Дмитрий"' in text


def test_render_tree_text_omits_value_when_empty():
    """An empty value must NOT render as value="" — keeps the tree clean."""
    root = make_element(0, "", "Pane", children=[1])
    edit = UIAElement(
        id=1, runtime_id="rid-1", automation_id="", name="Ваше имя",
        control_type="Edit", bounds=(0, 0, 10, 10), parent_id=0,
        parent_path="", children_ids=[], value="",
    )
    snap = UIASnapshot(windows=[], elements=[root, edit], first_window_id=None)
    text = render_tree_text(snap)
    assert 'value=' not in text


def test_snapshot_dict_roundtrip_preserves_value():
    from gc_computer_mcp.core.uia_types import snapshot_from_dict, snapshot_to_dict

    snap = UIASnapshot(
        windows=[],
        elements=[
            UIAElement(
                id=0, runtime_id="r0", automation_id="", name="Ваше имя",
                control_type="Edit", bounds=(0, 0, 10, 10), parent_id=None,
                parent_path="", children_ids=[], value="Дмитрий",
            ),
        ],
        first_window_id=None, timestamp="t",
    )
    d = snapshot_to_dict(snap)
    assert d["elements"][0]["value"] == "Дмитрий"
    restored = snapshot_from_dict(d)
    assert restored.elements[0].value == "Дмитрий"


def test_snapshot_dict_roundtrip_with_scope_hwnd():
    from gc_computer_mcp.core.uia_types import (
        UIASnapshot,
        snapshot_from_dict,
        snapshot_to_dict,
    )

    snap = UIASnapshot(
        windows=[], elements=[], first_window_id=None, timestamp="t",
    )
    snap.scope_hwnd = 42424
    d = snapshot_to_dict(snap)
    assert d["scope_hwnd"] == 42424
    restored = snapshot_from_dict(d)
    assert getattr(restored, "scope_hwnd", None) == 42424
