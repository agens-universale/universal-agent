"""AuthEditor v2: ●/○-состояния, initial-курсор на действующем варианте,
поле токена ПОД пунктом «Bearer-токен» (маска исчезает при вводе), allow_skip."""
import pytest

pytest.importorskip("textual")

from textual.app import App, ComposeResult
from textual.widgets import Input, Static


def _harness(editor):
    class _App(App):
        def compose(self) -> ComposeResult:
            yield editor
    return _App()


def _rows(ed):
    return [str(w.render()) for w in ed.query(".onb-option")]


def test_active_option_index_maps_current_header():
    from gc_tui.editors.auth import active_option_index
    from gc_core import onboarding
    assert active_option_index(None) is None
    assert active_option_index(onboarding.TOKEN_SENTINEL) is None
    assert active_option_index("") == 1
    assert active_option_index("Bearer x") == 0


async def test_auth_editor_bearer_active_marked_and_mask_shown():
    from gc_tui.editors.auth import AuthEditor
    ed = AuthEditor(current_header="Bearer secret123")
    app = _harness(ed)
    async with app.run_test():
        rows = _rows(ed)
        assert rows[0].startswith("▸ ● Bearer-токен")
        assert "sec***" in rows[0]
        assert rows[1].startswith("  ○ без авторизации")
        assert ed._idx == 0                          # initial-курсор на действующем


async def test_auth_editor_no_auth_active_initial_cursor_on_it():
    from gc_tui.editors.auth import AuthEditor
    ed = AuthEditor(current_header="")
    app = _harness(ed)
    async with app.run_test():
        rows = _rows(ed)
        assert rows[0].startswith("  ○ Bearer-токен")
        assert rows[1].startswith("▸ ● без авторизации")
        assert ed._idx == 1


async def test_auth_editor_no_current_header_no_marker_cursor_at_zero():
    from gc_tui.editors.auth import AuthEditor
    ed = AuthEditor(current_header=None)
    app = _harness(ed)
    async with app.run_test():
        rows = _rows(ed)
        assert rows[0].startswith("▸ ○ Bearer-токен")   # никто не действующий → ○
        assert rows[1].startswith("  ○ без авторизации")
        assert ed._idx == 0


async def test_auth_editor_mask_disappears_after_typing():
    from gc_tui.editors.auth import AuthEditor
    ed = AuthEditor(current_header="Bearer secret123")
    app = _harness(ed)
    async with app.run_test() as pilot:
        assert "sec***" in _rows(ed)[0]
        ed.query_one(Input).value = "new-token"
        await pilot.pause()
        assert "sec***" not in _rows(ed)[0]


async def test_auth_editor_children_order_option_input_option():
    from gc_tui.editors.auth import AuthEditor
    from gc_tui.widgets.paste_input import PasteInput
    ed = AuthEditor(current_header=None)
    app = _harness(ed)
    async with app.run_test():
        children = list(ed.children)
        assert isinstance(children[0], Static)
        assert isinstance(children[1], PasteInput)
        assert isinstance(children[2], Static)
        assert "auth-nested" in children[1].classes


async def test_auth_editor_allow_skip_false_has_two_options():
    from gc_tui.editors.auth import AuthEditor
    ed = AuthEditor(current_header=None, allow_skip=False)
    app = _harness(ed)
    async with app.run_test():
        assert len(_rows(ed)) == 2


async def test_auth_editor_allow_skip_true_has_three_options():
    from gc_tui.editors.auth import AuthEditor
    ed = AuthEditor(current_header=None, allow_skip=True)
    app = _harness(ed)
    async with app.run_test():
        rows = _rows(ed)
        assert len(rows) == 3
        assert rows[2].endswith("пропустить")


async def test_auth_editor_submit_bearer_with_input_returns_header_and_raw():
    from gc_tui.editors.auth import AuthEditor
    ed = AuthEditor(current_header=None)
    app = _harness(ed)
    async with app.run_test():
        ed.query_one(Input).value = "secret123"
        ok, header, raw = ed.submit()
        assert ok and header == "Bearer secret123" and raw == "secret123"


async def test_auth_editor_submit_bearer_empty_returns_none():
    from gc_tui.editors.auth import AuthEditor
    ed = AuthEditor(current_header="Bearer old-token")
    app = _harness(ed)
    async with app.run_test():
        ok, header, raw = ed.submit()      # Bearer действующий, поле пусто → не менять
        assert ok and header is None and raw == ""


async def test_auth_editor_bearer_empty_no_active_asks_for_token():
    from gc_tui.editors.auth import AuthEditor
    ed = AuthEditor(current_header=None)          # действующего токена нет
    app = _harness(ed)
    async with app.run_test():
        ok, header, raw = ed.submit()             # idx 0 (Bearer), поле пусто
        assert ok is False and header is None
        assert any("введите токен" in str(w.render()) for w in ed.query(".onb-warn"))


async def test_auth_editor_submit_no_auth_returns_empty_header():
    from gc_tui.editors.auth import AuthEditor
    ed = AuthEditor(current_header=None)
    app = _harness(ed)
    async with app.run_test():
        ed.move(1)                       # → «без авторизации»
        ok, header, raw = ed.submit()
        assert ok and header == "" and raw == ""
        assert not ed.query_one(Input).display   # токен-поле скрыто


async def test_auth_editor_submit_skip_returns_none():
    from gc_tui.editors.auth import AuthEditor
    ed = AuthEditor(current_header=None, allow_skip=True)
    app = _harness(ed)
    async with app.run_test():
        ed.move(2)                       # → «пропустить»
        ok, header, raw = ed.submit()
        assert ok and header is None and raw == ""


async def test_auth_editor_move_wraps_within_available_options():
    from gc_tui.editors.auth import AuthEditor
    ed = AuthEditor(current_header=None, allow_skip=True)
    app = _harness(ed)
    async with app.run_test():
        n = len(_rows(ed))
        for _ in range(n):
            ed.move(1)                   # полный круг
        assert ed._idx == 0
        ok, header, _ = ed.submit()      # снова Bearer, действующего токена нет → ошибка
        assert ed._idx == 0 and ok is False


async def test_auth_editor_peek_no_side_effects():
    from gc_tui.editors.auth import AuthEditor
    ed = AuthEditor(current_header=None)
    app = _harness(ed)
    async with app.run_test():
        assert ed.peek() == (0, "")
        ed.query_one(Input).value = "abc"
        assert ed.peek() == (0, "abc")
        assert ed.peek() == (0, "abc")   # повторный вызов идемпотентен
