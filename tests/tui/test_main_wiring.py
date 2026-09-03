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
        def set_config_path(self, path):   # как у настоящего TuiApp: путь + снимок mtime
            self._config_path = path
        def _maybe_notice_profile_activation(self, *a, **k):   # cold-start notice §3.5 — no-op
            pass
        def run(self):
            return None

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)
    monkeypatch.setattr("mcp_daemon_autostart.ensure_mcp_daemons", lambda cfg: None)
    monkeypatch.setattr("mcp_daemon_autostart.shutdown_mcp_daemons", lambda: None)

    from tui import main
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

    from tui import app as tui_app

    class _FakeApp:
        def __init__(self, **kw):
            pass

        def set_config_path(self, path):   # как у настоящего TuiApp: путь + снимок mtime
            self._config_path = path

        def _maybe_notice_profile_activation(self, *a, **k):   # cold-start notice §3.5 — no-op
            pass

        def run(self):
            return None

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)

    calls: list[str] = []
    monkeypatch.setattr(
        "mcp_daemon_autostart.ensure_mcp_daemons",
        lambda cfg: calls.append("ensure"),
    )
    monkeypatch.setattr(
        "mcp_daemon_autostart.shutdown_mcp_daemons",
        lambda: calls.append("shutdown"),
    )

    from tui import main

    cfg_path = _cfg_for_main(tmp_path)
    rc = main(argv=["--config", str(cfg_path)])
    assert rc == 0
    assert calls == ["ensure", "shutdown"]


def test_main_no_auto_mcp_skips_daemons(monkeypatch, tmp_path):
    pytest.importorskip("textual")

    from tui import app as tui_app

    class _FakeApp:
        def __init__(self, **kw):
            pass

        def set_config_path(self, path):   # как у настоящего TuiApp: путь + снимок mtime
            self._config_path = path

        def _maybe_notice_profile_activation(self, *a, **k):   # cold-start notice §3.5 — no-op
            pass

        def run(self):
            return None

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)

    calls: list[str] = []
    monkeypatch.setattr(
        "mcp_daemon_autostart.ensure_mcp_daemons",
        lambda cfg: calls.append("ensure"),
    )
    monkeypatch.setattr(
        "mcp_daemon_autostart.shutdown_mcp_daemons",
        lambda: calls.append("shutdown"),
    )

    from tui import main

    cfg_path = _cfg_for_main(tmp_path)
    rc = main(argv=["--config", str(cfg_path), "--no-auto-mcp"])
    assert rc == 0
    assert calls == []


def test_main_auto_mcp_flag_still_works(monkeypatch, tmp_path):
    """Явный -a совместим с дефолтом (ensure+shutdown)."""
    pytest.importorskip("textual")

    from tui import app as tui_app

    class _FakeApp:
        def __init__(self, **kw):
            pass

        def set_config_path(self, path):   # как у настоящего TuiApp: путь + снимок mtime
            self._config_path = path

        def _maybe_notice_profile_activation(self, *a, **k):   # cold-start notice §3.5 — no-op
            pass

        def run(self):
            return None

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)

    calls: list[str] = []
    monkeypatch.setattr(
        "mcp_daemon_autostart.ensure_mcp_daemons",
        lambda cfg: calls.append("ensure"),
    )
    monkeypatch.setattr(
        "mcp_daemon_autostart.shutdown_mcp_daemons",
        lambda: calls.append("shutdown"),
    )

    from tui import main

    cfg_path = _cfg_for_main(tmp_path)
    rc = main(argv=["--config", str(cfg_path), "-a"])
    assert rc == 0
    assert calls == ["ensure", "shutdown"]


def test_main_voice_autostart_does_not_block_startup(monkeypatch, tmp_path):
    """Регрессия находки ревью (2026-08-06): раньше main() ждал
    `ensure_voice_service` СИНХРОННО перед `app.run()` — на холодном старте
    speaches это до 90 с пустого терминала, пользователь решал, что программа
    зависла. Теперь main() только выставляет флаг ожидания на app — сам подъём
    и его вердикт (тост «не запустился» или тишина) уходят в TuiApp.on_mount,
    фоном, уже после первого кадра."""
    pytest.importorskip("textual")

    from tui import app as tui_app
    import voice_runtime.autostart as autostart
    import voice_runtime.state as voice_state
    from voice_runtime.state import VoiceState

    created: list = []

    class _FakeApp:
        def __init__(self, **kw):
            self._voice_autostart_pending = False   # как у настоящего TuiApp.__init__
            created.append(self)

        def set_config_path(self, path):   # как у настоящего TuiApp: путь + снимок mtime
            self._config_path = path

        def _maybe_notice_profile_activation(self, *a, **k):   # cold-start notice §3.5 — no-op
            pass

        def run(self):
            return None

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)
    monkeypatch.setattr("mcp_daemon_autostart.ensure_mcp_daemons", lambda cfg: None)
    monkeypatch.setattr("mcp_daemon_autostart.shutdown_mcp_daemons", lambda: None)

    state = VoiceState(version="v1", root="/data/voice/speaches", port=8735, model="m")
    monkeypatch.setattr(voice_state, "load_state", lambda: state)
    monkeypatch.setattr(autostart, "url_is_ours", lambda cfg, port: True)

    def _must_not_be_called(*a, **kw):
        raise AssertionError("main() не должен звать ensure_voice_service синхронно")
    monkeypatch.setattr(autostart, "ensure_voice_service", _must_not_be_called)

    from tui import main

    cfg_path = _cfg_for_main(tmp_path)
    rc = main(argv=["--config", str(cfg_path)])
    assert rc == 0
    assert len(created) == 1
    assert created[0]._voice_autostart_pending is True   # гейт прошёл — вердикт ждёт TuiApp


def test_main_hands_the_profile_flag_to_the_app(monkeypatch, tmp_path):
    """`--profile` обязан доехать до приложения тем же значением, что ушло в
    загрузчик: `TuiApp._profile_override` отдаёт его каждому перечитыванию
    (`reload_config`), иначе флаг живёт до первого сохранения из /settings.
    Ветка `--init-config` его НЕ передаёт: чинилка читает и пишет пользовательский
    файл, и `--profile` там не в счёт (см. комментарий в main)."""
    pytest.importorskip("textual")

    from tui import app as tui_app
    called: dict = {}

    class _FakeApp:
        def __init__(self, **kw):
            called.update(kw)

        def set_config_path(self, path):   # как у настоящего TuiApp: путь + снимок mtime
            self._config_path = path

        def _maybe_notice_profile_activation(self, *a, **k):   # cold-start notice §3.5 — no-op
            pass

        def run(self):
            return None

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)
    monkeypatch.setattr("mcp_daemon_autostart.ensure_mcp_daemons", lambda cfg: None)
    monkeypatch.setattr("mcp_daemon_autostart.shutdown_mcp_daemons", lambda: None)

    from tui import main

    cfg_path = _cfg_for_main(tmp_path)
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "work.yml").write_text("slots: {}\n", encoding="utf-8")
    rc = main(argv=["--config", str(cfg_path), "--profile", "work"])
    assert rc == 0
    assert called.get("profile_override") == "work"
