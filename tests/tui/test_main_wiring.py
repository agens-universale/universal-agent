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

    from tui import app as tui_app
    called: dict = {}

    class _FakeApp:
        def __init__(self, **kw):
            called.update(kw)
        def run(self):
            return None

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)

    from tui import main
    rc = main(argv=["--config", str(cfg_path)])
    assert rc == 0
    # Path comparison should be OS-tolerant (Windows backslash vs YAML's POSIX forward slash)
    import os
    expected = os.path.normpath(str(threads_dir))
    got = os.path.normpath(str(called.get("threads_dir") or ""))
    assert got == expected, f"got={got!r} expected={expected!r}"
    assert called.get("system") == "cli"


def test_main_auto_mcp_starts_and_stops_daemons(monkeypatch, tmp_path):
    pytest.importorskip("textual")

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

    from tui import app as tui_app

    class _FakeApp:
        def __init__(self, **kw):
            pass

        def run(self):
            return None

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)

    calls: list[str] = []

    def _ensure(cfg):
        calls.append("ensure")

    def _shutdown():
        calls.append("shutdown")

    monkeypatch.setattr("mcp_daemon_autostart.ensure_mcp_daemons", _ensure)
    monkeypatch.setattr("mcp_daemon_autostart.shutdown_mcp_daemons", _shutdown)

    from tui import main

    rc = main(argv=["--config", str(cfg_path), "-a"])
    assert rc == 0
    assert calls == ["ensure", "shutdown"]
