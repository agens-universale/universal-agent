from unittest.mock import MagicMock

import pytest

from computer_mcp.core import input as input_mod


def test_click_calls_pyautogui(monkeypatch):
    fake_pag = MagicMock()
    monkeypatch.setattr(input_mod, "pyautogui", fake_pag)
    input_mod.click(100, 200, button="left", clicks=2)
    fake_pag.click.assert_called_once_with(x=100, y=200, button="left", clicks=2)


def test_type_text_dispatches_to_send_unicode(monkeypatch):
    """On Windows, type_text calls _send_unicode with the text verbatim."""
    monkeypatch.setattr(input_mod.sys, "platform", "win32")
    calls: list[str] = []
    monkeypatch.setattr(input_mod, "_send_unicode", lambda t: calls.append(t))

    input_mod.type_text("Привет, мир!")

    assert calls == ["Привет, мир!"]


def test_type_text_empty_string_is_noop(monkeypatch):
    monkeypatch.setattr(input_mod.sys, "platform", "win32")
    calls: list[str] = []
    monkeypatch.setattr(input_mod, "_send_unicode", lambda t: calls.append(t))

    input_mod.type_text("")

    assert calls == []


def test_type_text_non_windows_falls_back_to_typewrite(monkeypatch):
    """On non-Windows (CI / Linux dev box), SendInput is unavailable;
    fall back to pyautogui.typewrite. Pragmatic — we target Windows in prod.
    """
    monkeypatch.setattr(input_mod.sys, "platform", "linux")
    fake_pag = MagicMock()
    monkeypatch.setattr(input_mod, "pyautogui", fake_pag)
    fake_send = MagicMock()
    monkeypatch.setattr(input_mod, "_send_unicode", fake_send)

    input_mod.type_text("hello")

    fake_send.assert_not_called()
    fake_pag.typewrite.assert_called_once_with("hello")


def test_hotkey_dispatches_to_vk_sequence_on_windows(monkeypatch):
    """On Windows, hotkey(['ctrl','v']) must send a Win32 SendInput
    sequence: ctrl-down, v-down, v-up, ctrl-up — via _send_vk_sequence.
    pyautogui's layer is skipped because its keyDown/keyUp timing with
    PAUSE=0 misses in Qt/Electron apps (observed: Ctrl+V in Telegram)."""
    monkeypatch.setattr(input_mod.sys, "platform", "win32")
    fake_pag = MagicMock()
    monkeypatch.setattr(input_mod, "pyautogui", fake_pag)
    sent: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        input_mod, "_send_vk_sequence", lambda events: sent.extend(events),
    )

    input_mod.hotkey(["ctrl", "v"])

    # VK_CONTROL = 0x11, VK 'V' = 0x56
    assert sent == [(0x11, False), (0x56, False), (0x56, True), (0x11, True)]
    fake_pag.hotkey.assert_not_called()


def test_press_key_dispatches_to_vk_sequence_on_windows(monkeypatch):
    """On Windows, press_key('enter') goes through _send_vk_sequence with
    one key-down followed by one key-up."""
    monkeypatch.setattr(input_mod.sys, "platform", "win32")
    fake_pag = MagicMock()
    monkeypatch.setattr(input_mod, "pyautogui", fake_pag)
    sent: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        input_mod, "_send_vk_sequence", lambda events: sent.extend(events),
    )

    input_mod.press_key("enter")

    # VK_RETURN = 0x0D
    assert sent == [(0x0D, False), (0x0D, True)]
    fake_pag.press.assert_not_called()


def test_hotkey_resolves_keys_case_insensitively(monkeypatch):
    monkeypatch.setattr(input_mod.sys, "platform", "win32")
    sent: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        input_mod, "_send_vk_sequence", lambda events: sent.extend(events),
    )

    input_mod.hotkey(["CTRL", "S"])

    # VK_CONTROL = 0x11, VK 'S' = 0x53
    assert sent == [(0x11, False), (0x53, False), (0x53, True), (0x11, True)]


def test_hotkey_unknown_key_raises(monkeypatch):
    monkeypatch.setattr(input_mod.sys, "platform", "win32")
    monkeypatch.setattr(
        input_mod, "_send_vk_sequence", lambda events: None,
    )
    with pytest.raises(ValueError):
        input_mod.hotkey(["ctrl", "no-such-key"])


def test_press_key_unknown_key_raises(monkeypatch):
    monkeypatch.setattr(input_mod.sys, "platform", "win32")
    monkeypatch.setattr(
        input_mod, "_send_vk_sequence", lambda events: None,
    )
    with pytest.raises(ValueError):
        input_mod.press_key("nonexistent")


def test_press_key_non_windows_falls_back_to_pyautogui(monkeypatch):
    """On non-Windows (Linux CI), SendInput is unavailable — pyautogui still
    used as fallback so tests that mock at the pyautogui layer keep working."""
    monkeypatch.setattr(input_mod.sys, "platform", "linux")
    fake_pag = MagicMock()
    monkeypatch.setattr(input_mod, "pyautogui", fake_pag)
    input_mod.press_key("enter")
    fake_pag.press.assert_called_once_with("enter")


def test_hotkey_non_windows_falls_back_to_pyautogui(monkeypatch):
    monkeypatch.setattr(input_mod.sys, "platform", "linux")
    fake_pag = MagicMock()
    monkeypatch.setattr(input_mod, "pyautogui", fake_pag)
    input_mod.hotkey(["ctrl", "s"])
    fake_pag.hotkey.assert_called_once_with("ctrl", "s")


def test_scroll_direction_down(monkeypatch):
    fake_pag = MagicMock()
    monkeypatch.setattr(input_mod, "pyautogui", fake_pag)
    input_mod.scroll("down", amount=3)
    fake_pag.scroll.assert_called_once()
    args, _ = fake_pag.scroll.call_args
    assert args[0] < 0  # negative for down


def test_scroll_direction_up(monkeypatch):
    fake_pag = MagicMock()
    monkeypatch.setattr(input_mod, "pyautogui", fake_pag)
    input_mod.scroll("up", amount=3)
    fake_pag.scroll.assert_called_once()
    args, _ = fake_pag.scroll.call_args
    assert args[0] > 0


def test_scroll_direction_right(monkeypatch):
    fake_pag = MagicMock()
    monkeypatch.setattr(input_mod, "pyautogui", fake_pag)
    input_mod.scroll("right", amount=3)
    fake_pag.hscroll.assert_called_once()
    args, _ = fake_pag.hscroll.call_args
    assert args[0] > 0


def test_scroll_direction_left(monkeypatch):
    fake_pag = MagicMock()
    monkeypatch.setattr(input_mod, "pyautogui", fake_pag)
    input_mod.scroll("left", amount=3)
    fake_pag.hscroll.assert_called_once()
    args, _ = fake_pag.hscroll.call_args
    assert args[0] < 0


def test_scroll_unknown_direction_raises(monkeypatch):
    fake_pag = MagicMock()
    monkeypatch.setattr(input_mod, "pyautogui", fake_pag)
    with pytest.raises(ValueError):
        input_mod.scroll("diagonal")


def test_drag_calls_moveTo_then_dragTo(monkeypatch):
    fake_pag = MagicMock()
    monkeypatch.setattr(input_mod, "pyautogui", fake_pag)
    input_mod.drag((100, 200), (300, 400), button="left")
    fake_pag.moveTo.assert_called_once_with(100, 200)
    fake_pag.dragTo.assert_called_once()
    args, kwargs = fake_pag.dragTo.call_args
    # pyautogui.dragTo(x, y, button=...)
    assert args[0] == 300 and args[1] == 400
    assert kwargs.get("button") == "left"


def test_move_to_calls_pyautogui(monkeypatch):
    fake_pag = MagicMock()
    monkeypatch.setattr(input_mod, "pyautogui", fake_pag)
    input_mod.move_to(500, 600)
    fake_pag.moveTo.assert_called_once_with(500, 600)
