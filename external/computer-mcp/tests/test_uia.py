from collections.abc import Iterable
from dataclasses import dataclass, field

from computer_mcp.core.uia import build_tree, find_by_runtime_id, find_by_selector


@dataclass
class FakeNode:
    runtime_id: str
    name: str
    control_type: str
    bounds: tuple[int, int, int, int]
    automation_id: str = ""
    children: list["FakeNode"] = field(default_factory=list)
    visible: bool = True
    value: str = ""


class FakeProvider:
    def __init__(self, root: FakeNode, hwnd_map: dict[int, FakeNode] | None = None):
        self.root = root
        self.hwnd_map = hwnd_map or {}

    def desktop_root(self) -> FakeNode:
        return self.root

    def iter_children(self, node: FakeNode) -> Iterable[FakeNode]:
        return list(node.children)

    def is_visible(self, node: FakeNode) -> bool:
        return node.visible

    def find_by_runtime_id(self, rid: str, scope_hwnd: int | None = None):
        if scope_hwnd is not None:
            root = self.hwnd_map.get(scope_hwnd)
            if root is None:
                return None
        else:
            root = self.root

        def walk(n):
            if n.runtime_id == rid:
                return n
            for c in n.children:
                r = walk(c)
                if r is not None:
                    return r
            return None
        return walk(root)

    def node_from_hwnd(self, hwnd: int):
        return self.hwnd_map.get(hwnd)


def make_tree():
    btn = FakeNode("rid-btn", "Save", "Button", (100, 100, 80, 30))
    txt = FakeNode("rid-txt", "A1", "Edit", (200, 100, 60, 30))
    hidden = FakeNode("rid-hid", "Tooltip", "Text", (0, 0, 0, 0), visible=False)
    win = FakeNode("rid-win", "Excel", "Window", (0, 0, 1920, 1080),
                   children=[btn, txt, hidden])
    root = FakeNode("rid-root", "Desktop", "Pane", (0, 0, 1920, 1080), children=[win])
    return root


def test_build_tree_assigns_ids_dfs():
    root = make_tree()
    snap = build_tree(FakeProvider(root), include_invisible=False)
    ids = [e.id for e in snap.elements]
    assert ids == list(range(len(snap.elements)))
    # Root is [0], window [1], its visible children [2] and [3]
    assert snap.elements[0].control_type == "Pane"
    assert snap.elements[1].control_type == "Window"
    names = [e.name for e in snap.elements]
    assert "Tooltip" not in names


def test_build_tree_includes_invisible_when_flag_set():
    root = make_tree()
    snap = build_tree(FakeProvider(root), include_invisible=True)
    names = [e.name for e in snap.elements]
    assert "Tooltip" in names


def test_find_by_runtime_id_returns_live_node():
    root = make_tree()
    provider = FakeProvider(root)
    found = find_by_runtime_id(provider, "rid-btn")
    assert found is not None
    assert found.name == "Save"


def test_find_by_runtime_id_returns_none_when_gone():
    root = make_tree()
    provider = FakeProvider(root)
    assert find_by_runtime_id(provider, "rid-unknown") is None


def test_find_by_selector_by_name_and_type():
    root = make_tree()
    provider = FakeProvider(root)
    found = find_by_selector(
        provider,
        name="Save",
        control_type="Button",
        automation_id="",
        parent_path="Desktop/Excel",
    )
    assert found is not None
    assert found.runtime_id == "rid-btn"


def test_find_by_selector_returns_none_when_no_match():
    root = make_tree()
    provider = FakeProvider(root)
    assert find_by_selector(provider, name="Nope", control_type="Button",
                            automation_id="", parent_path="") is None


def test_provider_node_from_hwnd_returns_mapped_node():
    root = make_tree()
    # Pretend HWND 42 points at the Excel window node
    excel_window = next(c for c in root.children if c.name == "Excel")
    provider = FakeProvider(root, hwnd_map={42: excel_window})

    node = provider.node_from_hwnd(42)
    assert node is not None
    assert node.name == "Excel"


def test_provider_node_from_hwnd_returns_none_for_unknown():
    root = make_tree()
    provider = FakeProvider(root)  # empty hwnd_map

    assert provider.node_from_hwnd(999) is None


def test_find_by_runtime_id_scoped_to_hwnd():
    """When scope_hwnd is passed, search starts at node_from_hwnd(hwnd), not desktop."""
    from computer_mcp.core.uia import find_by_runtime_id

    root = make_tree()
    excel = next(c for c in root.children if c.name == "Excel")
    provider = FakeProvider(root, hwnd_map={100: excel})

    # Target button lives under Excel window
    found = find_by_runtime_id(provider, "rid-btn", scope_hwnd=100)
    assert found is not None
    assert found.name == "Save"


def test_find_by_runtime_id_scoped_returns_none_if_not_in_scope():
    """If element exists under desktop but NOT under the scoped hwnd subtree,
    return None. Caller may fall back to desktop scope explicitly."""
    from computer_mcp.core.uia import find_by_runtime_id

    btn_a = FakeNode("rid-a", "A", "Button", (0, 0, 10, 10))
    win_a = FakeNode("rid-wa", "WindowA", "Window", (0, 0, 100, 100), children=[btn_a])
    btn_b = FakeNode("rid-b", "B", "Button", (0, 0, 10, 10))
    win_b = FakeNode("rid-wb", "WindowB", "Window", (0, 0, 100, 100), children=[btn_b])
    root = FakeNode("rid-r", "Desktop", "Pane", (0, 0, 100, 100), children=[win_a, win_b])
    provider = FakeProvider(root, hwnd_map={1: win_a})  # only WindowA scoped

    # Look for B's runtime_id but scope to WindowA → None
    found = find_by_runtime_id(provider, "rid-b", scope_hwnd=1)
    assert found is None
    # Desktop-scope finds it
    found2 = find_by_runtime_id(provider, "rid-b")
    assert found2 is not None and found2.name == "B"


def test_find_by_selector_scoped_to_hwnd():
    from computer_mcp.core.uia import find_by_selector

    btn_a = FakeNode("rid-a", "Save", "Button", (0, 0, 10, 10))
    win_a = FakeNode("rid-wa", "WindowA", "Window", (0, 0, 100, 100), children=[btn_a])
    btn_b = FakeNode("rid-b", "Save", "Button", (0, 0, 10, 10))
    win_b = FakeNode("rid-wb", "WindowB", "Window", (0, 0, 100, 100), children=[btn_b])
    root = FakeNode("rid-r", "Desktop", "Pane", (0, 0, 100, 100), children=[win_a, win_b])
    provider = FakeProvider(root, hwnd_map={1: win_a})

    found = find_by_selector(
        provider, name="Save", control_type="Button",
        automation_id="", parent_path="", scope_hwnd=1,
    )
    assert found is not None and found.runtime_id == "rid-a"


def test_find_by_selector_scoped_invalid_hwnd_returns_none():
    from computer_mcp.core.uia import find_by_selector

    root = make_tree()
    provider = FakeProvider(root)  # no hwnd_map entry

    found = find_by_selector(
        provider, name="Save", control_type="Button",
        automation_id="", parent_path="", scope_hwnd=999,
    )
    assert found is None


def test_build_tree_from_foreground_uses_foreground_hwnd(monkeypatch):
    """build_tree_from_foreground picks foreground HWND, resolves to node, walks."""
    from computer_mcp.core import uia as uia_mod

    root = make_tree()
    excel = next(c for c in root.children if c.name == "Excel")
    provider = FakeProvider(root, hwnd_map={777: excel})
    monkeypatch.setattr(uia_mod, "_get_foreground_hwnd", lambda: 777)

    snap = uia_mod.build_tree_from_foreground(provider)
    names = {e.name for e in snap.elements}
    assert "Excel" in names
    assert "Desktop" not in names


def test_build_tree_from_foreground_falls_back_to_desktop_when_no_foreground(monkeypatch):
    """If GetForegroundWindow returns 0, fall back to desktop walk."""
    from computer_mcp.core import uia as uia_mod

    root = make_tree()
    provider = FakeProvider(root)
    monkeypatch.setattr(uia_mod, "_get_foreground_hwnd", lambda: 0)

    snap = uia_mod.build_tree_from_foreground(provider)
    names = {e.name for e in snap.elements}
    assert "Desktop" in names


def test_build_tree_from_window_finds_by_title_substring():
    """build_tree_from_window matches top-level windows by substring (case-insensitive)."""
    from computer_mcp.core.uia import build_tree_from_window

    root = make_tree()
    provider = FakeProvider(root)
    snap = build_tree_from_window(provider, window_title="exc")
    names = {e.name for e in snap.elements}
    assert "Excel" in names
    assert "Desktop" not in names


def test_build_tree_from_window_returns_empty_when_no_match():
    from computer_mcp.core.uia import build_tree_from_window

    root = make_tree()
    provider = FakeProvider(root)
    snap = build_tree_from_window(provider, window_title="Nonexistent")
    assert snap.elements == []
    assert snap.windows == []


def test_build_tree_skips_empty_text_children_of_button():
    """TextControl без имени внутри Button — шум (иконка, невидимый лейбл).
    Такие ноды в дереве калькулятора заставляли модель кликать по ним вместо
    кнопок; клик по TextControl не регистрируется калькулятором."""
    icon = FakeNode("rid-icon", "", "TextControl", (10, 10, 20, 20))
    btn = FakeNode(
        "rid-btn", "Плюс", "ButtonControl", (0, 0, 40, 40), children=[icon],
    )
    root = FakeNode(
        "rid-root", "Root", "Pane", (0, 0, 100, 100), children=[btn],
    )
    snap = build_tree(FakeProvider(root), include_invisible=False)
    types = [e.control_type for e in snap.elements]
    assert "TextControl" not in types
    assert any(e.name == "Плюс" and e.control_type == "ButtonControl"
               for e in snap.elements)


def test_build_tree_skips_text_child_whose_name_matches_parent_button():
    """TextControl с именем == Button.Name — это визуальная метка, её
    безопасно прятать, потому что родитель-Button уже несёт ту же инфу."""
    label = FakeNode("rid-lbl", "Сохранить", "TextControl", (10, 10, 20, 20))
    btn = FakeNode(
        "rid-btn", "Сохранить", "ButtonControl", (0, 0, 40, 40),
        children=[label],
    )
    root = FakeNode("rid-r", "Root", "Pane", (0, 0, 100, 100), children=[btn])
    snap = build_tree(FakeProvider(root), include_invisible=False)
    types = [e.control_type for e in snap.elements]
    assert "TextControl" not in types


def test_build_tree_keeps_text_child_with_distinct_name():
    """TextControl с ИНОЙ подписью — полезная информация, не прячем.
    Edge-case: динамические лейблы внутри Button (иконка-значок и т.п.)."""
    dynamic = FakeNode(
        "rid-dyn", "3 новых", "TextControl", (10, 10, 20, 20),
    )
    btn = FakeNode(
        "rid-btn", "Уведомления", "ButtonControl", (0, 0, 40, 40),
        children=[dynamic],
    )
    root = FakeNode("rid-r", "Root", "Pane", (0, 0, 100, 100), children=[btn])
    snap = build_tree(FakeProvider(root), include_invisible=False)
    names = [e.name for e in snap.elements]
    assert "3 новых" in names


def test_build_tree_skips_empty_image_child_of_button():
    """ImageControl-иконка внутри Button — тот же тип шума что TextControl."""
    icon = FakeNode("rid-img", "", "ImageControl", (5, 5, 16, 16))
    btn = FakeNode(
        "rid-btn", "Меню", "ButtonControl", (0, 0, 40, 40), children=[icon],
    )
    root = FakeNode("rid-r", "Root", "Pane", (0, 0, 100, 100), children=[btn])
    snap = build_tree(FakeProvider(root), include_invisible=False)
    types = [e.control_type for e in snap.elements]
    assert "ImageControl" not in types


def test_build_tree_keeps_top_level_text_controls():
    """Top-level TextControl (например "Калькулятор обычный" как label окна)
    НЕ дочерний Button — должен остаться в снимке."""
    hdr = FakeNode("rid-h", "Калькулятор", "TextControl", (0, 0, 200, 20))
    root = FakeNode("rid-r", "Root", "Pane", (0, 0, 800, 600), children=[hdr])
    snap = build_tree(FakeProvider(root), include_invisible=False)
    assert any(
        e.name == "Калькулятор" and e.control_type == "TextControl"
        for e in snap.elements
    )


def test_build_tree_skips_text_under_list_item_too():
    """Фильтр должен применяться не только к Button: ListItem / MenuItem /
    TabItem / Hyperlink — такой же паттерн 'вложенный Text — это лейбл'."""
    inner = FakeNode("rid-inner", "", "TextControl", (10, 10, 20, 20))
    item = FakeNode(
        "rid-item", "Инженерный", "ListItemControl", (0, 0, 200, 30),
        children=[inner],
    )
    root = FakeNode("rid-r", "Menu", "List", (0, 0, 200, 600), children=[item])
    snap = build_tree(FakeProvider(root), include_invisible=False)
    types = [e.control_type for e in snap.elements]
    assert "TextControl" not in types


def test_build_tree_captures_value_from_edit_like_node():
    """FakeNode.value is threaded through to UIAElement.value in the snapshot."""
    edit = FakeNode(
        "rid-edit", "Ваше имя", "Edit", (0, 0, 100, 20), value="Дмитрий",
    )
    win = FakeNode(
        "rid-win", "Form", "Window", (0, 0, 800, 600), children=[edit],
    )
    root = FakeNode(
        "rid-r", "Desktop", "Pane", (0, 0, 800, 600), children=[win],
    )
    snap = build_tree(FakeProvider(root), include_invisible=False)
    by_name = {e.name: e for e in snap.elements}
    assert by_name["Ваше имя"].value == "Дмитрий"
    # Non-edit nodes default to empty value.
    assert by_name["Form"].value == ""
    assert by_name["Desktop"].value == ""


def test_build_tree_from_node_starts_walk_at_given_root():
    """build_tree_from_node should walk from the provided node, not desktop root."""
    from computer_mcp.core.uia import build_tree_from_node

    root = make_tree()
    provider = FakeProvider(root)
    # Excel window is a child of desktop root
    excel_window = next(c for c in root.children if c.name == "Excel")

    snap = build_tree_from_node(provider, excel_window, include_invisible=False)
    names = {e.name for e in snap.elements}
    # Desktop is NOT in the tree — we started from Excel.
    assert "Desktop" not in names
    assert "Excel" in names
    # Save button belongs to Excel window — still present.
    assert "Save" in names
