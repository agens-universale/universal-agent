"""End-to-end: foreground-scoped get_state + subsequent click/drag/type
must all use the saved scope_hwnd, not a desktop-wide walk."""

from unittest.mock import MagicMock

import pytest

from gc_computer_mcp.core.state_store import StateStore
from gc_computer_mcp.core.uia_types import UIAElement, UIASnapshot, snapshot_to_dict
from gc_computer_mcp.tools.keyboard import type_text as tool_type
from gc_computer_mcp.tools.mouse import click as tool_click
from gc_computer_mcp.tools.mouse import drag as tool_drag


@pytest.fixture
def tmp_temp(monkeypatch, tmp_path):
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    return tmp_path


def test_foreground_get_state_then_click_uses_scoped_walk(tmp_temp):
    """After get_state(scope='foreground'), a subsequent click must call
    provider.find_by_runtime_id with the saved scope_hwnd, not with None."""
    store = StateStore(session_id="integ", ttl_seconds=3600)

    snap = UIASnapshot(
        windows=[{"id": 0, "title": "SaluteJazz", "bounds": [0, 0, 800, 600]}],
        elements=[
            UIAElement(
                id=0, runtime_id="rid-root", automation_id="", name="SaluteJazz",
                control_type="Window", bounds=(0, 0, 800, 600), parent_id=None,
                parent_path="", children_ids=[1],
            ),
            UIAElement(
                id=1, runtime_id="rid-btn", automation_id="", name="Создать видеовстречу",
                control_type="Button", bounds=(100, 100, 200, 50), parent_id=0,
                parent_path="SaluteJazz", children_ids=[],
            ),
        ],
        first_window_id=0,
        timestamp="t",
    )
    snap.scope_hwnd = 987
    store.save(snapshot_to_dict(snap))

    provider = MagicMock()
    live = MagicMock()
    live.bounds = (100, 100, 200, 50)
    live.name = "Создать видеовстречу"
    live.control_type = "Button"
    provider.find_by_runtime_id.return_value = live

    fake_input = MagicMock()

    result = tool_click(
        element_id=1, provider=provider, store=store, _input=fake_input,
    )

    # CRITICAL: find_by_runtime_id must receive scope_hwnd=987
    provider.find_by_runtime_id.assert_called_once_with("rid-btn", scope_hwnd=987)
    assert result["state_changed_hint"] is True
    fake_input.click.assert_called_once()


def test_foreground_get_state_then_drag_in_paint_uses_scoped_walk(tmp_temp):
    """Paint scenario: after get_state(scope='foreground'), a subsequent
    drag from canvas-point-A to canvas-point-B must resolve BOTH element_ids
    through the saved scope_hwnd, and hit fake_input.drag with the correct
    from/to coordinates (centers of each element's bounds)."""
    store = StateStore(session_id="paint", ttl_seconds=3600)

    snap = UIASnapshot(
        windows=[{"id": 0, "title": "Безымянный — Paint", "bounds": [0, 0, 1200, 800]}],
        elements=[
            UIAElement(
                id=0, runtime_id="rid-root", automation_id="", name="Безымянный — Paint",
                control_type="Window", bounds=(0, 0, 1200, 800), parent_id=None,
                parent_path="", children_ids=[1, 2],
            ),
            # Two synthetic drag targets inside the canvas — a rect's corners.
            UIAElement(
                id=1, runtime_id="rid-p1", automation_id="", name="canvas-tl",
                control_type="Pane", bounds=(200, 200, 20, 20), parent_id=0,
                parent_path="Paint", children_ids=[],
            ),
            UIAElement(
                id=2, runtime_id="rid-p2", automation_id="", name="canvas-br",
                control_type="Pane", bounds=(600, 500, 20, 20), parent_id=0,
                parent_path="Paint", children_ids=[],
            ),
        ],
        first_window_id=0,
        timestamp="t",
    )
    snap.scope_hwnd = 4242
    store.save(snapshot_to_dict(snap))

    provider = MagicMock()
    live_tl = MagicMock()
    live_tl.bounds = (200, 200, 20, 20)
    live_tl.name = "canvas-tl"
    live_tl.control_type = "Pane"
    live_br = MagicMock()
    live_br.bounds = (600, 500, 20, 20)
    live_br.name = "canvas-br"
    live_br.control_type = "Pane"

    def resolve(rid, scope_hwnd=None):
        return {"rid-p1": live_tl, "rid-p2": live_br}.get(rid)

    provider.find_by_runtime_id.side_effect = resolve

    fake_input = MagicMock()

    result = tool_drag(
        from_element_id=1, to_element_id=2,
        provider=provider, store=store, _input=fake_input,
    )

    # Both resolves must have been scoped to the Paint window.
    assert provider.find_by_runtime_id.call_count == 2
    for call in provider.find_by_runtime_id.call_args_list:
        assert call.kwargs.get("scope_hwnd") == 4242
    # Centers: TL -> (210, 210), BR -> (610, 510).
    fake_input.drag.assert_called_once_with((210, 210), (610, 510), button="left")
    assert result["state_changed_hint"] is True


def test_foreground_get_state_then_type_unicode_in_notepad(tmp_temp):
    """Notepad scenario: type_text with a Cyrillic string must
    (1) resolve the EditControl through scope_hwnd,
    (2) click its center to focus,
    (3) pass the exact unicode string to fake_input.type_text,
    (4) skip the clear path when clear=False (default)."""
    store = StateStore(session_id="np", ttl_seconds=3600)

    snap = UIASnapshot(
        windows=[{"id": 0, "title": "Блокнот", "bounds": [0, 0, 800, 600]}],
        elements=[
            UIAElement(
                id=0, runtime_id="rid-root", automation_id="", name="Блокнот",
                control_type="Window", bounds=(0, 0, 800, 600), parent_id=None,
                parent_path="", children_ids=[1],
            ),
            UIAElement(
                id=1, runtime_id="rid-edit", automation_id="", name="Text editor",
                control_type="Edit", bounds=(0, 40, 800, 520), parent_id=0,
                parent_path="Блокнот", children_ids=[], value="",
            ),
        ],
        first_window_id=0,
        timestamp="t",
    )
    snap.scope_hwnd = 8080
    store.save(snapshot_to_dict(snap))

    provider = MagicMock()
    live = MagicMock()
    live.bounds = (0, 40, 800, 520)
    live.name = "Text editor"
    live.control_type = "Edit"
    provider.find_by_runtime_id.return_value = live

    fake_input = MagicMock()

    result = tool_type(
        element_id=1, text="Привет, мир!",
        provider=provider, store=store, _input=fake_input,
    )

    # Scoped resolve.
    provider.find_by_runtime_id.assert_called_once_with("rid-edit", scope_hwnd=8080)
    # Focused via click at the center of the edit: (400, 300).
    fake_input.click.assert_called_once_with(400, 300, clicks=1)
    # clear=False (default) — no ctrl+a / delete should fire.
    fake_input.hotkey.assert_not_called()
    fake_input.press_key.assert_not_called()
    # Exact unicode string passed through to the typing backend.
    fake_input.type_text.assert_called_once_with("Привет, мир!")
    assert result["typed_chars"] == len("Привет, мир!")
    assert result["state_changed_hint"] is True


def test_type_with_clear_prefers_value_pattern_over_ctrl_a(tmp_temp):
    """Regression for 'ВидеовстречаПланёрка по проекту' collision in Jazz:
    model passed clear=True, tool ran Ctrl+A/Delete, but React's async focus
    made the shortcut miss the Edit control so only the new text was typed
    and it appended to the existing value.

    Fix: tool tries UIA ValuePattern.SetValue('') FIRST (reliable in
    Electron). Keyboard-simulated Ctrl+A/Delete is kept as fallback only
    for controls without ValuePattern."""
    store = StateStore(session_id="np2", ttl_seconds=3600)

    snap = UIASnapshot(
        windows=[],
        elements=[
            UIAElement(
                id=1, runtime_id="rid-edit", automation_id="", name="Название встречи",
                control_type="Edit", bounds=(0, 0, 200, 30), parent_id=None,
                parent_path="", children_ids=[], value="Видеовстреча",
            ),
        ],
        first_window_id=None,
        timestamp="t",
    )
    snap.scope_hwnd = 9090
    store.save(snapshot_to_dict(snap))

    # live element with a working ValuePattern — mimics real UIA on Electron.
    set_value_calls: list[str] = []

    class _Pattern:
        def SetValue(self, v):
            set_value_calls.append(v)

    class _Raw:
        def GetValuePattern(self):
            return _Pattern()

    class _Live:
        bounds = (0, 0, 200, 30)
        name = "Название встречи"
        control_type = "Edit"
        raw = _Raw()

    provider = MagicMock()
    provider.find_by_runtime_id.return_value = _Live()

    fake_input = MagicMock()

    tool_type(
        element_id=1, text="Планёрка", clear=True,
        provider=provider, store=store, _input=fake_input,
    )

    # UIA path taken: SetValue('') called exactly once.
    assert set_value_calls == [""]
    # Keyboard fallback MUST NOT run — that's the whole point of the fix.
    fake_input.hotkey.assert_not_called()
    fake_input.press_key.assert_not_called()
    # Normal focus click + text typing still happen.
    fake_input.click.assert_called_once()
    fake_input.type_text.assert_called_once_with("Планёрка")


def test_type_with_clear_falls_back_to_ctrl_a_when_value_pattern_missing(tmp_temp):
    """For controls WITHOUT ValuePattern (classic Win32 password boxes, some
    custom controls), the tool must fall back to Ctrl+A/Delete — otherwise
    clear=True would silently do nothing."""
    store = StateStore(session_id="np3", ttl_seconds=3600)

    snap = UIASnapshot(
        windows=[],
        elements=[
            UIAElement(
                id=1, runtime_id="rid-edit", automation_id="", name="Password",
                control_type="Edit", bounds=(0, 0, 200, 30), parent_id=None,
                parent_path="", children_ids=[], value="",
            ),
        ],
        first_window_id=None,
        timestamp="t",
    )
    store.save(snapshot_to_dict(snap))

    class _LiveNoRaw:
        bounds = (0, 0, 200, 30)
        name = "Password"
        control_type = "Edit"
        # No `raw` attribute → _try_clear_via_value_pattern returns False.

    provider = MagicMock()
    provider.find_by_runtime_id.return_value = _LiveNoRaw()

    fake_input = MagicMock()

    tool_type(
        element_id=1, text="secret", clear=True,
        provider=provider, store=store, _input=fake_input,
    )

    # Keyboard fallback order: focus click → ctrl+a → delete → type_text.
    method_order = [c[0] for c in fake_input.method_calls]
    assert method_order == ["click", "hotkey", "press_key", "type_text"]
    fake_input.hotkey.assert_called_once_with(["ctrl", "a"])
    fake_input.press_key.assert_called_once_with("delete")
    fake_input.type_text.assert_called_once_with("secret")
