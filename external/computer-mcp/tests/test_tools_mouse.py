from unittest.mock import MagicMock

import pytest

from computer_mcp.core.state_store import StateStore
from computer_mcp.errors import ComputerMCPError, ErrorCategory
from computer_mcp.tools.mouse import click as tool_click


class StubElement:
    def __init__(self, bounds=(100, 200, 80, 30), name="Save", ctype="Button"):
        self.bounds = bounds
        self.name = name
        self.control_type = ctype
        self.runtime_id = "rid-1"
        self.automation_id = ""


def _snapshot():
    return {
        "windows": [],
        "first_window_id": None,
        "timestamp": "",
        "elements": [
            {
                "id": 3, "runtime_id": "rid-1", "automation_id": "",
                "name": "Save", "control_type": "Button",
                "bounds": [100, 200, 80, 30], "parent_id": None,
                "parent_path": "Desktop/Excel", "children_ids": [],
            }
        ],
    }


def test_click_fast_path_calls_input(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    store = StateStore("t", 3600)
    store.save(_snapshot())

    provider = MagicMock()
    live = StubElement()
    provider.find_by_runtime_id.return_value = live

    fake_input = MagicMock()
    result = tool_click(
        element_id=3, button="left", clicks=1,
        provider=provider, store=store,
        _input=fake_input,
    )
    fake_input.click.assert_called_once()
    args, kwargs = fake_input.click.call_args
    # bounds center: (100 + 40, 200 + 15) = (140, 215)
    # _input.click is called with positional (x, y, button=, clicks=) OR kwargs
    x_val = kwargs.get("x", args[0] if args else None)
    y_val = kwargs.get("y", args[1] if len(args) > 1 else None)
    assert x_val == 140
    assert y_val == 215
    assert result["clicked"].startswith("Button")
    assert result["state_changed_hint"] is True


def test_click_slow_path_when_runtime_id_gone(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    store = StateStore("t2", 3600)
    store.save(_snapshot())

    provider = MagicMock()
    provider.find_by_runtime_id.return_value = None
    stub_live = StubElement()

    fake_input = MagicMock()
    tool_click(
        element_id=3,
        provider=provider, store=store,
        _input=fake_input,
        _find_by_selector=lambda **kwargs: stub_live,
    )
    fake_input.click.assert_called_once()


def test_click_element_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    store = StateStore("t3", 3600)
    store.save(_snapshot())

    provider = MagicMock()
    provider.find_by_runtime_id.return_value = None

    with pytest.raises(ComputerMCPError) as ex:
        tool_click(
            element_id=3, provider=provider, store=store,
            _input=MagicMock(),
            _find_by_selector=lambda **kwargs: None,
        )
    assert ex.value.category == ErrorCategory.ELEMENT_NOT_FOUND


def test_click_stale_state_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    store = StateStore("missing", 3600)
    with pytest.raises(ComputerMCPError) as ex:
        tool_click(element_id=3, provider=MagicMock(), store=store, _input=MagicMock())
    assert ex.value.category == ErrorCategory.STALE_STATE


def test_click_unknown_element_id_in_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    store = StateStore("t4", 3600)
    store.save(_snapshot())

    with pytest.raises(ComputerMCPError) as ex:
        tool_click(
            element_id=999,
            provider=MagicMock(), store=store, _input=MagicMock(),
        )
    assert ex.value.category == ErrorCategory.ELEMENT_NOT_FOUND


def test_resolve_element_uses_scope_hwnd_from_snapshot(tmp_path, monkeypatch):
    """If the snapshot contains scope_hwnd, _resolve_element passes it through."""
    from computer_mcp.tools.mouse import _resolve_element

    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    store = StateStore("rshwnd", 3600)
    snap = _snapshot()
    snap["scope_hwnd"] = 777
    store.save(snap)

    provider = MagicMock()
    stub_live = MagicMock()
    provider.find_by_runtime_id.return_value = stub_live

    _resolve_element(element_id=3, provider=provider, store=store)
    provider.find_by_runtime_id.assert_called_with("rid-1", scope_hwnd=777)


def test_resolve_element_falls_back_to_desktop_when_scoped_fails(tmp_path, monkeypatch):
    """If scoped resolve returns None for both fast + slow paths, fall back to
    desktop-scope. Mirrors the spec section 8.3 three-tier cascade."""
    from computer_mcp.tools.mouse import _resolve_element

    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    store = StateStore("rfb", 3600)
    snap = _snapshot()
    snap["scope_hwnd"] = 777
    store.save(snap)

    provider = MagicMock()
    stub_live_desktop = MagicMock()

    def find_by_rid(rid, scope_hwnd=None):
        return None if scope_hwnd else stub_live_desktop
    provider.find_by_runtime_id.side_effect = find_by_rid

    def selector_always_none(**kwargs):
        return None

    _, live = _resolve_element(
        element_id=3, provider=provider, store=store,
        find_by_selector=selector_always_none,
    )
    assert live is stub_live_desktop
    # Called TWICE: once with scope, once without.
    assert provider.find_by_runtime_id.call_count == 2
