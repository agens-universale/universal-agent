from unittest.mock import MagicMock

import pytest

from computer_mcp.core.state_store import StateStore
from computer_mcp.errors import ComputerMCPError, ErrorCategory
from computer_mcp.tools.keyboard import key as tool_key
from computer_mcp.tools.keyboard import shortcut as tool_shortcut
from computer_mcp.tools.keyboard import type_text as tool_type


class StubLive:
    def __init__(self):
        self.bounds = (100, 200, 60, 30)


def _snapshot():
    return {
        "windows": [], "first_window_id": None, "timestamp": "",
        "elements": [{
            "id": 7, "runtime_id": "rid-x", "automation_id": "",
            "name": "A1", "control_type": "Edit",
            "bounds": [100, 200, 60, 30], "parent_id": None,
            "parent_path": "", "children_ids": [],
        }],
    }


def test_type_clicks_then_types(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    store = StateStore("kt1", 3600)
    store.save(_snapshot())

    provider = MagicMock()
    provider.find_by_runtime_id.return_value = StubLive()

    fake_input = MagicMock()
    res = tool_type(
        element_id=7, text="hello", clear=False, press_enter=False,
        provider=provider, store=store, _input=fake_input,
    )
    fake_input.click.assert_called()  # focus the field first
    fake_input.type_text.assert_called_once_with("hello")
    assert res["typed_chars"] == 5


def test_type_with_clear_sends_ctrl_a_then_del(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    store = StateStore("kt2", 3600)
    store.save(_snapshot())
    provider = MagicMock()
    provider.find_by_runtime_id.return_value = StubLive()

    fake_input = MagicMock()
    tool_type(
        element_id=7, text="x", clear=True, press_enter=False,
        provider=provider, store=store, _input=fake_input,
    )
    fake_input.hotkey.assert_any_call(["ctrl", "a"])
    fake_input.press_key.assert_any_call("delete")


def test_type_with_press_enter_presses_enter_after(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    store = StateStore("kt3", 3600)
    store.save(_snapshot())
    provider = MagicMock()
    provider.find_by_runtime_id.return_value = StubLive()

    fake_input = MagicMock()
    tool_type(
        element_id=7, text="q", clear=False, press_enter=True,
        provider=provider, store=store, _input=fake_input,
    )
    fake_input.press_key.assert_called_with("enter")


def test_key_calls_press_key():
    fake_input = MagicMock()
    tool_key(key="f5", _input=fake_input)
    fake_input.press_key.assert_called_once_with("f5")


def test_shortcut_allows_ctrl_s():
    fake_input = MagicMock()
    tool_shortcut(keys=["ctrl", "s"], _input=fake_input)
    fake_input.hotkey.assert_called_once_with(["ctrl", "s"])


def test_shortcut_blocks_ctrl_alt_del():
    fake_input = MagicMock()
    with pytest.raises(ComputerMCPError) as ex:
        tool_shortcut(keys=["ctrl", "alt", "delete"], _input=fake_input)
    assert ex.value.category == ErrorCategory.BLACKLISTED_KEYS
    fake_input.hotkey.assert_not_called()
