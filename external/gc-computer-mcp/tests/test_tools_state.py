import time

import pytest

from gc_computer_mcp.core.state_store import StateStore
from gc_computer_mcp.core.uia_types import UIAElement, UIASnapshot
from gc_computer_mcp.tools.state import get_state, wait


def test_get_state_returns_text_tree_and_saves_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    store = StateStore(session_id="t1", ttl_seconds=3600)

    # Use a minimal concrete provider by patching build_tree directly.
    fake_snap = UIASnapshot(
        windows=[{"id": 1, "title": "Excel", "bounds": [0, 0, 1920, 1080]}],
        elements=[
            UIAElement(0, "r0", "", "", "Pane", (0, 0, 100, 100), None, "", [1]),
            UIAElement(1, "r1", "", "Excel", "Window", (0, 0, 100, 100), 0, "", []),
        ],
        first_window_id=1,
        timestamp="2026-04-19T18:00:00+00:00",
    )

    result = get_state(
        provider=object(),  # unused because we patch build_tree
        store=store,
        scope="desktop",
        include_invisible=False,
        _build_tree=lambda p, include_invisible: fake_snap,
    )
    assert "[0] Pane" in result["tree_text"]
    assert "[1] Window" in result["tree_text"]
    loaded = store.load()
    assert loaded["first_window_id"] == 1


def test_get_state_dispatches_to_foreground_scope(tmp_path, monkeypatch):
    """scope='foreground' routes to build_tree_from_foreground."""
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    store = StateStore(session_id="fs1", ttl_seconds=3600)

    called = {}

    def fake_foreground(provider, include_invisible=False):
        called["foreground"] = True
        return UIASnapshot(
            windows=[], elements=[], first_window_id=None, timestamp="t",
        )

    result = get_state(
        provider=object(), store=store, scope="foreground",
        _build_tree_from_foreground=fake_foreground,
    )
    assert called.get("foreground") is True
    assert "tree_text" in result


def test_get_state_dispatches_to_window_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    store = StateStore(session_id="ws1", ttl_seconds=3600)

    called = {}

    def fake_window(provider, window_title, include_invisible=False):
        called["window_title"] = window_title
        return UIASnapshot(
            windows=[], elements=[], first_window_id=None, timestamp="t",
        )

    get_state(
        provider=object(), store=store, scope="window", window_title="Блокнот",
        _build_tree_from_window=fake_window,
    )
    assert called["window_title"] == "Блокнот"


def test_get_state_window_scope_requires_window_title(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    store = StateStore(session_id="ws2", ttl_seconds=3600)

    from gc_computer_mcp.errors import ComputerMCPError, ErrorCategory

    with pytest.raises(ComputerMCPError) as ex:
        get_state(provider=object(), store=store, scope="window")
    assert ex.value.category == ErrorCategory.INVALID_INPUT


def test_get_state_exclude_shell_filters_taskbar_and_program_manager(tmp_path, monkeypatch):
    """exclude_shell=True drops 'Панель задач' and 'Program Manager' top-level panes."""
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    store = StateStore(session_id="es1", ttl_seconds=3600)

    snap = UIASnapshot(
        windows=[],
        elements=[
            UIAElement(0, "r0", "", "Desktop", "Pane", (0, 0, 100, 100), None, "", [1, 2, 3]),
            UIAElement(1, "r1", "", "Панель задач", "Pane", (0, 0, 100, 40), 0, "Desktop", []),
            UIAElement(2, "r2", "", "Program Manager", "Pane", (0, 0, 100, 40), 0, "Desktop", []),
            UIAElement(3, "r3", "", "Excel", "Window", (0, 0, 100, 100), 0, "Desktop", []),
        ],
        first_window_id=3, timestamp="t",
    )

    get_state(
        provider=object(), store=store, scope="desktop", exclude_shell=True,
        _build_tree=lambda p, include_invisible: snap,
    )
    # Snapshot saved to store — load it back and inspect.
    loaded = store.load()
    names = [e["name"] for e in loaded["elements"]]
    assert "Панель задач" not in names
    assert "Program Manager" not in names
    assert "Excel" in names
    assert "Desktop" in names


def test_get_state_default_scope_is_foreground(tmp_path, monkeypatch):
    """Calling get_state with no scope arg should route to foreground."""
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    store = StateStore(session_id="dfg", ttl_seconds=3600)

    called = {}

    def fake_foreground(provider, include_invisible=False):
        called["foreground"] = True
        return UIASnapshot(
            windows=[], elements=[], first_window_id=None, timestamp="t",
        )

    get_state(
        provider=object(), store=store,  # no scope arg
        _build_tree_from_foreground=fake_foreground,
    )
    assert called.get("foreground") is True


def test_get_state_stores_scope_hwnd_for_foreground(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    store = StateStore(session_id="sfg", ttl_seconds=3600)

    def fake_foreground(provider, include_invisible=False):
        s = UIASnapshot(
            windows=[], elements=[], first_window_id=None, timestamp="t",
        )
        s.scope_hwnd = 555
        return s

    get_state(
        provider=object(), store=store, scope="foreground",
        _build_tree_from_foreground=fake_foreground,
    )
    loaded = store.load()
    assert loaded["scope_hwnd"] == 555


def test_get_state_stores_null_scope_hwnd_for_desktop(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    store = StateStore(session_id="sds", ttl_seconds=3600)

    snap = UIASnapshot(windows=[], elements=[], first_window_id=None, timestamp="t")
    get_state(
        provider=object(), store=store, scope="desktop",
        _build_tree=lambda p, include_invisible: snap,
    )
    loaded = store.load()
    assert loaded.get("scope_hwnd") is None


def test_wait_sleeps_given_seconds():
    t0 = time.time()
    wait(seconds=0.1)
    assert time.time() - t0 >= 0.09


def test_wait_rejects_over_30():
    from gc_computer_mcp.errors import ComputerMCPError, ErrorCategory
    with pytest.raises(ComputerMCPError) as ex:
        wait(seconds=31)
    assert ex.value.category == ErrorCategory.INVALID_INPUT
