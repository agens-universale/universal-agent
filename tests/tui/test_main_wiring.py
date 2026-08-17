"""Сборка стека: main → load_config → threads_dir → TuiApp."""
import pytest


def test_main_assembles_stack_dry_run(monkeypatch, tmp_path):
    pytest.importorskip("textual")

    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        "backend:\n"
        "  api_mode: chat_completions\n"
        "  completions_url: http://127.0.0.1:9\n"
        "paths:\n"
        f'  threads_data_path: "{threads_dir.as_posix()}"\n'
        "logging:\n"
        "  console_trace: true\n",
        encoding="utf-8",
    )

    from gc_tui import app as tui_app
    called: dict = {}

    class _FakeApp:
        def __init__(self, **kw):
            called.update(kw)
        def set_config_path(self, path):   # как у настоящего TuiApp: путь + снимок mtime
            self._config_path = path
        def run(self):
            return None

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)
    monkeypatch.setattr("gc_core.mcp_daemon_autostart.ensure_mcp_daemons", lambda cfg: None)
    monkeypatch.setattr("gc_core.mcp_daemon_autostart.shutdown_mcp_daemons", lambda: None)

    from gc_tui import main
    rc = main(argv=["--config", str(cfg_path)])
    assert rc == 0
    # Path comparison should be OS-tolerant (Windows backslash vs YAML's POSIX forward slash)
    import os
    expected = os.path.normpath(str(threads_dir))
    got = os.path.normpath(str(called.get("threads_dir") or ""))
    assert got == expected, f"got={got!r} expected={expected!r}"
    assert called.get("system") == "cli"


def _cfg_for_main(tmp_path):
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        "backend:\n"
        "  api_mode: chat_completions\n"
        "  completions_url: http://127.0.0.1:9\n"
        "paths:\n"
        f'  threads_data_path: "{threads_dir.as_posix()}"\n',
        encoding="utf-8",
    )
    return cfg_path


def test_main_auto_mcp_default_starts_and_stops_daemons(monkeypatch, tmp_path):
    """gc-ua по умолчанию поднимает HTTP/SSE MCP-daemon'ы (без явного -a)."""
    pytest.importorskip("textual")

    from gc_tui import app as tui_app

    class _FakeApp:
        def __init__(self, **kw):
            pass

        def set_config_path(self, path):   # как у настоящего TuiApp: путь + снимок mtime
            self._config_path = path

        def run(self):
            return None

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)

    calls: list[str] = []
    monkeypatch.setattr(
        "gc_core.mcp_daemon_autostart.ensure_mcp_daemons",
        lambda cfg: calls.append("ensure"),
    )
    monkeypatch.setattr(
        "gc_core.mcp_daemon_autostart.shutdown_mcp_daemons",
        lambda: calls.append("shutdown"),
    )

    from gc_tui import main

    cfg_path = _cfg_for_main(tmp_path)
    rc = main(argv=["--config", str(cfg_path)])
    assert rc == 0
    assert calls == ["ensure", "shutdown"]


def test_main_no_auto_mcp_skips_daemons(monkeypatch, tmp_path):
    pytest.importorskip("textual")

    from gc_tui import app as tui_app

    class _FakeApp:
        def __init__(self, **kw):
            pass

        def set_config_path(self, path):   # как у настоящего TuiApp: путь + снимок mtime
            self._config_path = path

        def run(self):
            return None

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)

    calls: list[str] = []
    monkeypatch.setattr(
        "gc_core.mcp_daemon_autostart.ensure_mcp_daemons",
        lambda cfg: calls.append("ensure"),
    )
    monkeypatch.setattr(
        "gc_core.mcp_daemon_autostart.shutdown_mcp_daemons",
        lambda: calls.append("shutdown"),
    )

    from gc_tui import main

    cfg_path = _cfg_for_main(tmp_path)
    rc = main(argv=["--config", str(cfg_path), "--no-auto-mcp"])
    assert rc == 0
    assert calls == []


def test_main_auto_mcp_flag_still_works(monkeypatch, tmp_path):
    """Явный -a совместим с дефолтом (ensure+shutdown)."""
    pytest.importorskip("textual")

    from gc_tui import app as tui_app

    class _FakeApp:
        def __init__(self, **kw):
            pass

        def set_config_path(self, path):   # как у настоящего TuiApp: путь + снимок mtime
            self._config_path = path

        def run(self):
            return None

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)

    calls: list[str] = []
    monkeypatch.setattr(
        "gc_core.mcp_daemon_autostart.ensure_mcp_daemons",
        lambda cfg: calls.append("ensure"),
    )
    monkeypatch.setattr(
        "gc_core.mcp_daemon_autostart.shutdown_mcp_daemons",
        lambda: calls.append("shutdown"),
    )

    from gc_tui import main

    cfg_path = _cfg_for_main(tmp_path)
    rc = main(argv=["--config", str(cfg_path), "-a"])
    assert rc == 0
    assert calls == ["ensure", "shutdown"]
