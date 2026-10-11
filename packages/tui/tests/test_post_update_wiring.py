"""Wiring tests for the post-update screen push (спека D, Task 10).

`app._upgrade_info` is set by `tui/__init__.py` before `app.run()`. On start,
TuiApp must push PostUpdateScreen on top of the default screen when an
upgrade was detected (respecting onboarding order: onboarding first, if
needed), and must NOT push it when there was no upgrade.

Stamp semantics (`upgrade_gate.mark_current_run`): must NOT be called
unconditionally at TUI startup when an upgrade is in flight — PostUpdateScreen
itself stamps on close (see test_post_update_screen.py). tui/__init__.py is
covered separately below for the "no mark_current_run at startup during an
upgrade" wiring.
"""

from gc_testkit.tui_support import tui_settings
import sys

import pytest

pytest.importorskip("textual")

from gc_core import config_sync, upgrade_gate
from gc_tui.screens.post_update import PostUpdateScreen
from recommended_mcp.runtime import cli as mcp_cli_mod


def _stub_post_update_deps(monkeypatch):
    """PostUpdateScreen.__init__ reaches for wired MCP names + config-sync
    diff on construction — stub both so tests don't touch disk/subprocess."""
    monkeypatch.setattr(mcp_cli_mod, "wired_names", lambda config=None: [])
    monkeypatch.setattr(config_sync, "needs_sync", lambda *a, **k: False)


# ---------------------------------------------------------------------------
# Test 1: _upgrade_info set, no onboarding needed → PostUpdateScreen pushed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upgrade_info_set_pushes_post_update_screen(
    tmp_path, monkeypatch
):
    _stub_post_update_deps(monkeypatch)
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen

    cfg = {
        "backend.completions_url": "https://h/v1/chat/completions",
        "backend.chat_completion_headers": {"Authorization": "Bearer sk"},
        "agent.model": "m",
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    app._config_path = str(tmp_path / "config.yml")
    app._upgrade_info = upgrade_gate.UpgradeInfo(
        old_version="0.3.0", new_version="0.4.0"
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, PostUpdateScreen), (
            f"Expected PostUpdateScreen on top, got {type(app.screen).__name__}"
        )
        assert any(isinstance(s, TraceScreen) for s in app.screen_stack), (
            "Base TraceScreen must still be in the stack beneath the overlay"
        )


# ---------------------------------------------------------------------------
# Test 2: _upgrade_info None → PostUpdateScreen NOT pushed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_upgrade_info_no_post_update_screen(tmp_path, monkeypatch):
    _stub_post_update_deps(monkeypatch)
    from gc_tui.app import TuiApp

    cfg = {
        "backend.completions_url": "https://h/v1/chat/completions",
        "backend.chat_completion_headers": {"Authorization": "Bearer sk"},
        "agent.model": "m",
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    app._config_path = str(tmp_path / "config.yml")
    # app._upgrade_info stays None (default)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert not isinstance(app.screen, PostUpdateScreen)
        assert not any(
            isinstance(s, PostUpdateScreen) for s in app.screen_stack
        )


# ---------------------------------------------------------------------------
# Test 3: onboarding needed + upgrade → onboarding first, post-update after
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_onboarding_then_post_update_order(tmp_path, monkeypatch):
    _stub_post_update_deps(monkeypatch)
    from gc_core import config as cfg_mod
    from gc_core import thread_store as ts_mod
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen
    from gc_tui.screens.onboarding import OnboardingScreen

    configured = {
        "backend.completions_url": "https://h/v1/chat/completions",
        "backend.chat_completion_headers": {"Authorization": "Bearer sk"},
        "agent.model": "m",
    }
    from gc_core.config_modules.profiles import ProfileMeta
    from dataclasses import replace

    from gc_testkit.settings import make_settings

    monkeypatch.setattr(
        cfg_mod,
        "load_settings",
        lambda p=None, **kw: replace(
            make_settings(configured),
            profile_meta=ProfileMeta(active_name=None, ref=None, mtime_set={}),
        ),
    )
    monkeypatch.setattr(cfg_mod, "apply_cli_overrides", lambda d, **kw: d)
    monkeypatch.setattr(
        ts_mod, "resolve_threads_dir", lambda c: str(tmp_path / "t")
    )

    unconf = {
        "backend.completions_url": "http://your-backend:PORT/v1/chat/completions",
        "backend.chat_completion_headers": {
            "Authorization": "Bearer <ВПИШИТЕ_ТОКЕН>"
        },
        "agent.model": "<ВПИШИТЕ_МОДЕЛЬ>",
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(unconf)
    )
    app._onboarding_mode = "auto"
    app._onboarding_config_path = str(tmp_path / "config.yml")
    app._config_path = str(tmp_path / "config.yml")
    app._upgrade_info = upgrade_gate.UpgradeInfo(
        old_version="0.3.0", new_version="0.4.0"
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        # Onboarding first — post-update must NOT be up yet.
        assert isinstance(app.screen, OnboardingScreen)
        assert not any(
            isinstance(s, PostUpdateScreen) for s in app.screen_stack
        )

        app._finish_onboarding()
        await pilot.pause()
        assert isinstance(app.screen, PostUpdateScreen), (
            f"Expected PostUpdateScreen after onboarding finished, got {type(app.screen).__name__}"
        )
        assert any(isinstance(s, TraceScreen) for s in app.screen_stack)


# ---------------------------------------------------------------------------
# Test 4: idempotent — repeated calls don't stack duplicate screens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_show_post_update_is_idempotent(tmp_path, monkeypatch):
    _stub_post_update_deps(monkeypatch)
    from gc_tui.app import TuiApp

    cfg = {
        "backend.completions_url": "https://h/v1/chat/completions",
        "backend.chat_completion_headers": {"Authorization": "Bearer sk"},
        "agent.model": "m",
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    app._config_path = str(tmp_path / "config.yml")
    app._upgrade_info = upgrade_gate.UpgradeInfo(
        old_version="0.3.0", new_version="0.4.0"
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        app._show_post_update_if_needed()
        app._show_post_update_if_needed()
        await pilot.pause()
        count = sum(
            1 for s in app.screen_stack if isinstance(s, PostUpdateScreen)
        )
        assert count == 1


# ---------------------------------------------------------------------------
# Test 5: tui/__init__.py main() does NOT stamp at startup when an upgrade
# was just detected — stamp is deferred to PostUpdateScreen.on_unmount.
# ---------------------------------------------------------------------------


def _patch_entry_prereqs(monkeypatch, tmp_path, config_dict):
    from gc_core.config_modules import env_vars as env_mod

    monkeypatch.setattr(env_mod, "ensure_project_gc_dir", lambda: None)

    from gc_core import config as cfg_mod

    from gc_core.config_modules.profiles import ProfileMeta
    from dataclasses import replace

    from gc_testkit.settings import make_settings

    monkeypatch.setattr(
        cfg_mod,
        "load_settings",
        lambda path=None, **kw: replace(
            make_settings(config_dict),
            profile_meta=ProfileMeta(active_name=None, ref=None, mtime_set={}),
        ),
    )
    monkeypatch.setattr(cfg_mod, "apply_cli_overrides", lambda d, **kw: d)
    monkeypatch.setattr(
        cfg_mod, "user_config_path", lambda: tmp_path / "config.yml"
    )
    monkeypatch.setattr(cfg_mod, "_bootstrap_user_config", lambda: True)

    from gc_core import onboarding as ob_mod

    monkeypatch.setattr(ob_mod, "is_unconfigured", lambda cfg: False)

    from gc_core import thread_store as ts_mod

    monkeypatch.setattr(
        ts_mod, "resolve_threads_dir", lambda cfg: str(tmp_path / "threads")
    )

    from recommended_mcp.runtime import daemon_autostart as mcp_da_mod

    monkeypatch.setattr(
        mcp_da_mod, "ensure_mcp_daemons", lambda cfg, **kw: None, raising=False
    )
    monkeypatch.setattr(
        mcp_da_mod, "shutdown_mcp_daemons", lambda: None, raising=False
    )


def test_main_does_not_stamp_when_upgrade_detected(monkeypatch, tmp_path):
    import gc_tui as tui_mod

    configured = {
        "backend.completions_url": "https://h/v1/chat/completions",
        "backend.chat_completion_headers": {"Authorization": "Bearer sk"},
        "agent.model": "m",
    }
    _patch_entry_prereqs(monkeypatch, tmp_path, configured)

    monkeypatch.setattr(
        upgrade_gate,
        "detect_upgrade",
        lambda *a, **k: upgrade_gate.UpgradeInfo("0.3.0", "0.4.0"),
    )
    mark_calls = []
    monkeypatch.setattr(
        upgrade_gate, "mark_current_run", lambda *a, **k: mark_calls.append(1)
    )

    from gc_tui import app as app_mod

    OrigTuiApp = app_mod.TuiApp
    captured = {}

    class FakeTuiApp(OrigTuiApp):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            captured["app"] = self

        def run(self, **kwargs):
            captured["ran"] = True

    monkeypatch.setattr(app_mod, "TuiApp", FakeTuiApp)
    monkeypatch.setitem(sys.modules, "gc_tui.app", app_mod)

    result = tui_mod.main([])

    assert result == 0
    assert captured.get("ran") is True
    assert captured["app"]._upgrade_info == upgrade_gate.UpgradeInfo(
        "0.3.0", "0.4.0"
    )
    assert mark_calls == [], (
        "mark_current_run must NOT run at startup during an upgrade run"
    )


def test_main_stamps_when_no_upgrade(monkeypatch, tmp_path):
    """No upgrade this run — startup stamp still happens (baseline for future
    detect_upgrade calls; PostUpdateScreen is never mounted in this case)."""
    import gc_tui as tui_mod

    configured = {
        "backend.completions_url": "https://h/v1/chat/completions",
        "backend.chat_completion_headers": {"Authorization": "Bearer sk"},
        "agent.model": "m",
    }
    _patch_entry_prereqs(monkeypatch, tmp_path, configured)

    monkeypatch.setattr(upgrade_gate, "detect_upgrade", lambda *a, **k: None)
    mark_calls = []
    monkeypatch.setattr(
        upgrade_gate, "mark_current_run", lambda *a, **k: mark_calls.append(1)
    )

    from gc_tui import app as app_mod

    OrigTuiApp = app_mod.TuiApp
    captured = {}

    class FakeTuiApp(OrigTuiApp):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            captured["app"] = self

        def run(self, **kwargs):
            captured["ran"] = True

    monkeypatch.setattr(app_mod, "TuiApp", FakeTuiApp)
    monkeypatch.setitem(sys.modules, "gc_tui.app", app_mod)

    result = tui_mod.main([])

    assert result == 0
    assert captured["app"]._upgrade_info is None
    assert mark_calls == [1], (
        "no upgrade this run → startup stamp still happens"
    )
