from unittest.mock import MagicMock

import pytest

from gc_computer_mcp.errors import ComputerMCPError, ErrorCategory
from gc_computer_mcp.tools.windows import launch_app, resize, switch


def test_switch_calls_backend():
    backend = MagicMock()
    backend.switch.return_value = True
    res = switch(window_title="Excel", _backend=backend)
    backend.switch.assert_called_once_with("Excel")
    assert res["state_changed_hint"] is True


def test_switch_raises_when_not_found():
    backend = MagicMock()
    backend.switch.return_value = False
    with pytest.raises(ComputerMCPError) as ex:
        switch(window_title="Nope", _backend=backend)
    assert ex.value.category == ErrorCategory.APP_NOT_FOUND


def test_resize_minimize_calls_backend():
    backend = MagicMock()
    backend.minimize.return_value = True
    resize(window_title="Excel", minimize=True, _backend=backend)
    backend.minimize.assert_called_once_with("Excel")


def test_resize_with_explicit_dims():
    backend = MagicMock()
    backend.resize.return_value = True
    resize(window_title="Excel", x=0, y=0, width=800, height=600, _backend=backend)
    backend.resize.assert_called_once_with("Excel", 0, 0, 800, 600)


def test_launch_app_known_name():
    backend = MagicMock()
    backend.launch.return_value = True
    backend.probe_launched_window.return_value = (None, None)
    launch_app(app_name="excel", _backend=backend)
    backend.launch.assert_called_once_with("excel")


def test_launch_app_not_found():
    backend = MagicMock()
    backend.launch.return_value = False
    with pytest.raises(ComputerMCPError) as ex:
        launch_app(app_name="zzz", _backend=backend)
    assert ex.value.category == ErrorCategory.APP_NOT_FOUND


def test_launch_app_returns_window_info():
    """After spawning, probe discovers new window's title + hwnd via HWND-diff."""
    backend = MagicMock()
    backend.snapshot_hwnds.return_value = {100, 200}
    backend.launch.return_value = True
    backend.probe_launched_window.return_value = ("Microsoft Excel - Book1", 1234567)

    result = launch_app(app_name="excel", _backend=backend)
    assert result["launched"] == "excel"
    assert result["window_title"] == "Microsoft Excel - Book1"
    assert result["window_hwnd"] == 1234567


def test_launch_app_window_info_best_effort():
    """If probe fails (returns None), fields are None but call still succeeds."""
    backend = MagicMock()
    backend.snapshot_hwnds.return_value = set()
    backend.launch.return_value = True
    backend.probe_launched_window.return_value = (None, None)

    result = launch_app(app_name="calc", _backend=backend)
    assert result["window_title"] is None
    assert result["window_hwnd"] is None


def test_launch_app_snapshots_hwnds_before_launch():
    """launch_app must snapshot BEFORE calling backend.launch so probe can diff."""
    backend = MagicMock()
    backend.snapshot_hwnds.return_value = {100, 200}
    backend.launch.return_value = True
    backend.probe_launched_window.return_value = (None, None)

    call_order = []
    backend.snapshot_hwnds.side_effect = lambda: (call_order.append("snap"), {100, 200})[1]
    backend.launch.side_effect = lambda name: (call_order.append("launch"), True)[1]
    backend.probe_launched_window.side_effect = (
        lambda **kw: (call_order.append(("probe", kw.get("before"))), (None, None))[1]
    )

    launch_app(app_name="jazz", _backend=backend)
    assert call_order[0] == "snap"
    assert call_order[1] == "launch"
    assert call_order[2][0] == "probe"
    # probe received the pre-launch snapshot as `before`
    assert call_order[2][1] == {100, 200}


def test_is_dev_window_filters_agent_and_terminal_titles():
    """_is_dev_window returns True for agent/dev/terminal titles, False for real apps."""
    from gc_computer_mcp.tools.windows import _is_dev_window

    assert _is_dev_window("gc-cli — prompt") is True
    assert _is_dev_window("Windows PowerShell") is True
    assert _is_dev_window("Claude Code") is True
    assert _is_dev_window("Windows Terminal") is True
    assert _is_dev_window("Command Prompt") is True
    assert _is_dev_window("") is True  # empty titles are not useful

    # Real apps must NOT be filtered
    assert _is_dev_window("SaluteJazz") is False
    assert _is_dev_window("Paint") is False
    assert _is_dev_window("Блокнот") is False
    assert _is_dev_window("Microsoft Excel — Книга1") is False


def test_probe_launched_window_returns_new_non_dev_hwnd(monkeypatch):
    """HWND-diff: return the first new top-level HWND whose title isn't dev."""
    from gc_computer_mcp.tools import windows as win_mod

    monkeypatch.setattr(win_mod.sys, "platform", "win32")
    # New hwnd 500 appears; before had {100, 200}
    monkeypatch.setattr(
        win_mod, "_enum_top_level_hwnds", lambda: {100, 200, 500},
    )
    titles = {100: "Blah", 200: "Other", 500: "SaluteJazz"}
    monkeypatch.setattr(
        win_mod, "_get_window_title", lambda h: titles.get(h, ""),
    )

    backend = win_mod._DefaultBackend()
    title, hwnd = backend.probe_launched_window(timeout_s=0.5, before={100, 200})
    assert title == "SaluteJazz"
    assert hwnd == 500


def test_probe_launched_window_filters_dev_new_hwnd(monkeypatch):
    """A new HWND whose title is a dev window gets filtered — not returned."""
    from gc_computer_mcp.tools import windows as win_mod

    monkeypatch.setattr(win_mod.sys, "platform", "win32")
    monkeypatch.setattr(
        win_mod, "_enum_top_level_hwnds", lambda: {100, 200, 999},
    )
    # Only new hwnd 999 has title "gc-cli" — dev window, must be filtered.
    monkeypatch.setattr(
        win_mod, "_get_window_title", lambda h: {999: "gc-cli"}.get(h, ""),
    )

    backend = win_mod._DefaultBackend()
    title, hwnd = backend.probe_launched_window(timeout_s=0.1, before={100, 200})
    assert title is None
    assert hwnd is None


def test_probe_launched_window_returns_none_when_no_new_hwnds(monkeypatch):
    """No new HWND → (None, None), respecting the timeout."""
    from gc_computer_mcp.tools import windows as win_mod

    monkeypatch.setattr(win_mod.sys, "platform", "win32")
    monkeypatch.setattr(win_mod, "_enum_top_level_hwnds", lambda: {100, 200})
    monkeypatch.setattr(win_mod, "_get_window_title", lambda h: "Blah")

    backend = win_mod._DefaultBackend()
    title, hwnd = backend.probe_launched_window(timeout_s=0.1, before={100, 200})
    assert title is None
    assert hwnd is None


def test_snapshot_hwnds_empty_on_non_windows(monkeypatch):
    from gc_computer_mcp.tools import windows as win_mod

    monkeypatch.setattr(win_mod.sys, "platform", "linux")
    backend = win_mod._DefaultBackend()
    assert backend.snapshot_hwnds() == set()


def test_lookup_start_menu_exact_match(monkeypatch):
    from gc_computer_mcp.tools import windows as win_mod
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = (
        '[{"Name":"Jazz","AppID":"salutejazz.jazz-app"},'
        '{"Name":"Paint","AppID":"Microsoft.Paint_8wekyb3d8bbwe!App"}]'
    )
    monkeypatch.setattr(win_mod.shutil, "which", lambda x: "powershell.exe")
    monkeypatch.setattr(win_mod.subprocess, "run", lambda *a, **kw: fake_result)

    assert win_mod._lookup_start_menu("Jazz") == "salutejazz.jazz-app"


def test_lookup_start_menu_case_insensitive(monkeypatch):
    from gc_computer_mcp.tools import windows as win_mod
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = '[{"Name":"Jazz","AppID":"salutejazz.jazz-app"}]'
    monkeypatch.setattr(win_mod.shutil, "which", lambda x: "powershell.exe")
    monkeypatch.setattr(win_mod.subprocess, "run", lambda *a, **kw: fake_result)

    assert win_mod._lookup_start_menu("JAZZ") == "salutejazz.jazz-app"
    assert win_mod._lookup_start_menu("jazz") == "salutejazz.jazz-app"


def test_lookup_start_menu_substring_fallback(monkeypatch):
    from gc_computer_mcp.tools import windows as win_mod
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = '[{"Name":"SaluteJazz","AppID":"salutejazz.jazz-app"}]'
    monkeypatch.setattr(win_mod.shutil, "which", lambda x: "powershell.exe")
    monkeypatch.setattr(win_mod.subprocess, "run", lambda *a, **kw: fake_result)

    # Query "jazz" does not exact-match "SaluteJazz", but substring match should find it
    assert win_mod._lookup_start_menu("jazz") == "salutejazz.jazz-app"


def test_lookup_start_menu_single_item_dict(monkeypatch):
    """PowerShell returns a single object (not array) when only 1 match. Handle."""
    from gc_computer_mcp.tools import windows as win_mod
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = '{"Name":"Paint","AppID":"Microsoft.Paint_8wekyb3d8bbwe!App"}'
    monkeypatch.setattr(win_mod.shutil, "which", lambda x: "powershell.exe")
    monkeypatch.setattr(win_mod.subprocess, "run", lambda *a, **kw: fake_result)

    assert win_mod._lookup_start_menu("Paint") == "Microsoft.Paint_8wekyb3d8bbwe!App"


def test_lookup_start_menu_no_powershell(monkeypatch):
    from gc_computer_mcp.tools import windows as win_mod
    monkeypatch.setattr(win_mod.shutil, "which", lambda x: None)

    assert win_mod._lookup_start_menu("Jazz") is None


def test_lookup_start_menu_no_match(monkeypatch):
    from gc_computer_mcp.tools import windows as win_mod
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = '[{"Name":"Jazz","AppID":"salutejazz.jazz-app"}]'
    monkeypatch.setattr(win_mod.shutil, "which", lambda x: "powershell.exe")
    monkeypatch.setattr(win_mod.subprocess, "run", lambda *a, **kw: fake_result)

    assert win_mod._lookup_start_menu("Nonexistent") is None


def test_lookup_start_menu_needle_is_superstring_of_name(monkeypatch):
    """'SberJazz' should match Start Menu item named 'Jazz'
    (bidirectional substring — needle 'contains' the listed name)."""
    from gc_computer_mcp.tools import windows as win_mod
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = '[{"Name":"Jazz","AppID":"salutejazz.jazz-app"}]'
    monkeypatch.setattr(win_mod.shutil, "which", lambda x: "powershell.exe")
    monkeypatch.setattr(win_mod.subprocess, "run", lambda *a, **kw: fake_result)

    assert win_mod._lookup_start_menu("SberJazz") == "salutejazz.jazz-app"


def test_lookup_start_menu_camelcase_token_match(monkeypatch):
    """'SberJazz' should be tokenised to ['sber','jazz'] and any single token
    matching the listed Name counts as a hit (token-level fallback)."""
    from gc_computer_mcp.tools import windows as win_mod
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = '[{"Name":"Jazz","AppID":"salutejazz.jazz-app"}]'
    monkeypatch.setattr(win_mod.shutil, "which", lambda x: "powershell.exe")
    monkeypatch.setattr(win_mod.subprocess, "run", lambda *a, **kw: fake_result)

    assert win_mod._lookup_start_menu("SberJazz") == "salutejazz.jazz-app"


def test_spawn_uwp_uses_powershell_start_process(monkeypatch):
    """UWP apps must be launched via PowerShell Start-Process.
    Direct subprocess.Popen(['explorer.exe', 'shell:AppsFolder\\...']) does
    not trigger UWP activation when called from a Python child process."""
    from gc_computer_mcp.tools import windows as win_mod
    calls = []

    def fake_popen(cmd, shell=False, **kwargs):
        calls.append(cmd)
        return MagicMock()

    monkeypatch.setattr(
        win_mod.shutil, "which",
        lambda x: "powershell.exe" if x in ("powershell", "pwsh") else None,
    )
    monkeypatch.setattr(win_mod.subprocess, "Popen", fake_popen)

    assert win_mod._spawn_uwp("salutejazz.jazz-app") is True
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[0] == "powershell.exe"
    assert "-NoProfile" in cmd
    # The Start-Process invocation must carry the AppsFolder URI.
    cmd_text = " ".join(cmd)
    assert "Start-Process" in cmd_text
    assert "shell:AppsFolder\\salutejazz.jazz-app" in cmd_text


def test_spawn_uwp_returns_false_when_no_powershell(monkeypatch):
    from gc_computer_mcp.tools import windows as win_mod
    monkeypatch.setattr(win_mod.shutil, "which", lambda x: None)

    assert win_mod._spawn_uwp("any.app.id") is False


def test_launch_tier3_uses_spawn_uwp_for_start_menu_apps(monkeypatch):
    """When Start Menu lookup returns an AppID, launch must use _spawn_uwp
    (PowerShell Start-Process), NOT the plain _spawn(['explorer.exe', ...])."""
    from gc_computer_mcp.tools import windows as win_mod
    # Force tier 1 (aliases) and tier 2 (PATH) to miss.
    monkeypatch.setattr(win_mod.shutil, "which", lambda x: None)
    monkeypatch.setattr(
        win_mod, "_lookup_start_menu", lambda name: "salutejazz.jazz-app",
    )
    monkeypatch.setattr(win_mod.sys, "platform", "win32")

    uwp_called = {}

    def fake_uwp(app_id):
        uwp_called["app_id"] = app_id
        return True

    def fake_spawn(cmd):
        uwp_called["spawn_cmd"] = cmd
        return True

    monkeypatch.setattr(win_mod, "_spawn_uwp", fake_uwp)
    monkeypatch.setattr(win_mod, "_spawn", fake_spawn)

    backend = win_mod._DefaultBackend()
    assert backend.launch("Jazz") is True
    assert uwp_called.get("app_id") == "salutejazz.jazz-app"
    assert "spawn_cmd" not in uwp_called


def test_lookup_start_menu_exact_wins_over_fuzzy(monkeypatch):
    """If both an exact match and a fuzzy/token match exist, exact wins."""
    from gc_computer_mcp.tools import windows as win_mod
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = (
        '[{"Name":"Jazz","AppID":"salutejazz.jazz-app"},'
        '{"Name":"SberJazz","AppID":"exact.match.app"}]'
    )
    monkeypatch.setattr(win_mod.shutil, "which", lambda x: "powershell.exe")
    monkeypatch.setattr(win_mod.subprocess, "run", lambda *a, **kw: fake_result)

    assert win_mod._lookup_start_menu("SberJazz") == "exact.match.app"


def test_switch_finds_substring_and_calls_set_foreground(monkeypatch):
    """_DefaultBackend.switch must:
    - enumerate top-level HWNDs (not pygetwindow — which misses Telegram etc.),
    - match title by substring (case-insensitive),
    - ShowWindow(SW_RESTORE) then SetForegroundWindow on the first match.

    Regression: pygetwindow-based switch said app_not_found for Telegram
    even though get_state(scope="desktop") listed its window."""
    from gc_computer_mcp.tools import windows as win_mod

    monkeypatch.setattr(win_mod.sys, "platform", "win32")
    monkeypatch.setattr(
        win_mod, "_enum_top_level_hwnds", lambda: {100, 200, 777},
    )
    titles = {100: "File Explorer", 200: "Chrome", 777: "Maesthrow – Telegram"}
    monkeypatch.setattr(
        win_mod, "_get_window_title", lambda h: titles.get(h, ""),
    )

    calls: list[tuple[str, int, int | None]] = []

    class _FakeUser32:
        def ShowWindow(self, hwnd, cmd):
            calls.append(("ShowWindow", int(hwnd), int(cmd)))
            return 1

        def SetForegroundWindow(self, hwnd):
            calls.append(("SetForegroundWindow", int(hwnd), None))
            return 1

    monkeypatch.setattr(win_mod, "_user32", _FakeUser32())

    backend = win_mod._DefaultBackend()
    assert backend.switch("telegram") is True

    # Order: restore first (in case it's minimized), then foreground.
    assert calls[0] == ("ShowWindow", 777, 9)  # SW_RESTORE = 9
    assert calls[1] == ("SetForegroundWindow", 777, None)


def test_switch_returns_false_when_no_match(monkeypatch):
    from gc_computer_mcp.tools import windows as win_mod

    monkeypatch.setattr(win_mod.sys, "platform", "win32")
    monkeypatch.setattr(win_mod, "_enum_top_level_hwnds", lambda: {100, 200})
    monkeypatch.setattr(
        win_mod, "_get_window_title", lambda h: {100: "Chrome", 200: "Spotify"}.get(h, ""),
    )

    class _FakeUser32:
        def ShowWindow(self, *a, **kw):
            raise AssertionError("ShowWindow must not be called")

        def SetForegroundWindow(self, *a, **kw):
            raise AssertionError("SetForegroundWindow must not be called")

    monkeypatch.setattr(win_mod, "_user32", _FakeUser32())

    backend = win_mod._DefaultBackend()
    assert backend.switch("nonexistent") is False


def test_switch_skips_dev_windows_and_takes_next_match(monkeypatch):
    """If substring matches a dev window (gc-cli) AND a real app, prefer
    the real app — we should never activate our own terminal."""
    from gc_computer_mcp.tools import windows as win_mod

    monkeypatch.setattr(win_mod.sys, "platform", "win32")
    monkeypatch.setattr(win_mod, "_enum_top_level_hwnds", lambda: {10, 20})
    titles = {
        10: "gc-cli — prompt output",    # dev window, must be skipped
        20: "Windows PowerShell output", # also dev, but we match "output"
    }
    monkeypatch.setattr(
        win_mod, "_get_window_title", lambda h: titles.get(h, ""),
    )

    fg_calls: list[int] = []

    class _FakeUser32:
        def ShowWindow(self, hwnd, cmd): return 1
        def SetForegroundWindow(self, hwnd):
            fg_calls.append(int(hwnd))
            return 1

    monkeypatch.setattr(win_mod, "_user32", _FakeUser32())

    backend = win_mod._DefaultBackend()
    # Both match "output" but both are dev windows → False
    assert backend.switch("output") is False
    assert fg_calls == []
