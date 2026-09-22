"""Wiring tests for onboarding integration in tui/__init__.py (gc-ua entry).

These tests reach the onboarding decision point WITHOUT launching Textual
by monkeypatching the load_config + is_unconfigured path and driving main()
up to the early-return onboarding exits (which happen before alt-screen).
"""

import sys
import types

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_entry_prereqs(monkeypatch, tmp_path, config_dict, *, unconfigured=True):
    """
    Monkeypatch the prerequisites that main() calls before the onboarding block,
    so we never reach importlib.import_module('tui.app') or app.run().

    Returns the tui module (imported once so monkeypatches on it are stable).
    """
    import gc_tui as tui_mod

    # ensure_project_gc_dir — side-effectful, short-circuit it
    monkeypatch.setattr("gc_core.config_modules.env_vars.ensure_project_gc_dir", lambda: None, raising=False)
    # Patch tui's own reference to ensure_project_gc_dir (it imports lazily inside main)
    # We'll patch via the module that main() uses at runtime:
    from gc_core.config_modules import env_vars as env_mod

    monkeypatch.setattr(env_mod, "ensure_project_gc_dir", lambda: None)

    # Patch config functions used in main()
    from gc_core import config as cfg_mod
    from gc_core.config_modules.profiles import ProfileMeta

    monkeypatch.setattr(
        cfg_mod,
        "load_config_ex",
        lambda path=None, **kw: (dict(config_dict), ProfileMeta(active_name=None, ref=None, mtime_set={})),
    )
    monkeypatch.setattr(cfg_mod, "apply_cli_overrides", lambda d: None)
    monkeypatch.setattr(cfg_mod, "user_config_path", lambda: tmp_path / "config.yml")
    monkeypatch.setattr(cfg_mod, "_bootstrap_user_config", lambda: True)

    from gc_core import onboarding as ob_mod

    monkeypatch.setattr(ob_mod, "is_unconfigured", lambda cfg: unconfigured)
    monkeypatch.setattr(
        ob_mod,
        "setup_guide",
        lambda path, cfg, program="gc-ua", just_created=False: f"[gc] setup guide: edit {path}/config.yml",
    )

    return tui_mod


# ---------------------------------------------------------------------------
# Test 1: Non-TTY passive path
# ---------------------------------------------------------------------------


def test_non_tty_onboarding_prints_guide_and_returns_0(monkeypatch, tmp_path, capsys):
    """
    WIRING: when _interactive()→False and config is unconfigured,
    main() must print the setup guide to stderr and return 0 — no Textual.

    This exercises the 'else' branch of the new onboarding block:
        else:
            print(onboarding.setup_guide(...), file=sys.stderr)
            return 0
    """
    import gc_tui as tui_mod

    _patch_entry_prereqs(monkeypatch, tmp_path, config_dict={}, unconfigured=True)

    # Suppress tui.app import inside main() by providing a fake module
    fake_app_mod = types.ModuleType("gc_tui.app")
    monkeypatch.setitem(sys.modules, "gc_tui.app", fake_app_mod)

    # TTY gate → not interactive
    monkeypatch.setattr(tui_mod, "_interactive", lambda: False, raising=False)

    # main([]) сам парсит пустой argv → config=None ветка онбординга срабатывает,
    # init-config не активен. sys.argv подстрахован, чтобы argparse не читал реальный.
    monkeypatch.setattr(sys, "argv", ["gc-ua"])

    result = tui_mod.main([])  # pass empty argv list

    captured = capsys.readouterr()
    assert result == 0
    assert "config.yml" in captured.err


# ---------------------------------------------------------------------------
# Test 2: Interactive + unconfigured → app._onboarding_mode="auto", app.run() called
# ---------------------------------------------------------------------------


def test_interactive_unconfigured_sets_auto_mode(monkeypatch, tmp_path):
    """
    WIRING: when _interactive()→True and config is unconfigured,
    main() must set app._onboarding_mode='auto' and call app.run() — NOT return 0.

    The 'user quit' case is now handled inside the Textual app (on_commit not called).
    This replaces the old test that verified onboarding_wizard.run was called.
    """
    import gc_tui as tui_mod

    _patch_entry_prereqs(monkeypatch, tmp_path, config_dict={}, unconfigured=True)

    # TTY gate → interactive
    monkeypatch.setattr(tui_mod, "_interactive", lambda: True, raising=False)

    # resolve_threads_dir requires threads_data_path — patch it
    from gc_core import thread_store as ts_mod

    monkeypatch.setattr(ts_mod, "resolve_threads_dir", lambda cfg: str(tmp_path / "threads"))

    # Also suppress daemon_autostart (imported in main)
    from recommended_mcp.runtime import daemon_autostart as mcp_da_mod

    monkeypatch.setattr(mcp_da_mod, "ensure_mcp_daemons", lambda cfg, **kw: None, raising=False)
    monkeypatch.setattr(mcp_da_mod, "shutdown_mcp_daemons", lambda: None, raising=False)

    # Capture app attributes and intercept run()
    captured = {}
    from gc_tui import app as app_mod

    OrigTuiApp = app_mod.TuiApp

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
    assert captured.get("ran") is True, "app.run() должен быть вызван"
    assert captured["app"]._onboarding_mode == "auto", (
        f"Expected _onboarding_mode='auto', got {captured['app']._onboarding_mode!r}"
    )
