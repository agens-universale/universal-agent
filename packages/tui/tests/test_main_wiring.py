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
        def _maybe_notice_profile_activation(self, *a, **k):   # cold-start notice §3.5 — no-op
            pass
        def run(self):
            return None

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)
    monkeypatch.setattr("recommended_mcp.runtime.daemon_autostart.ensure_mcp_daemons", lambda cfg, **kw: None)
    monkeypatch.setattr("recommended_mcp.runtime.daemon_autostart.shutdown_mcp_daemons", lambda: None)

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

        def _maybe_notice_profile_activation(self, *a, **k):   # cold-start notice §3.5 — no-op
            pass

        def run(self):
            return None

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)

    calls: list[str] = []
    monkeypatch.setattr(
        "recommended_mcp.runtime.daemon_autostart.ensure_mcp_daemons",
        # **kw: у ensure_mcp_daemons есть именованный disable_hint — фронтенд
        # сообщает им, КАК у него выключается автозапуск (у gc-ua это
        # `--no-auto-mcp`, у gc-cli такого флага нет). Заглушка обязана его
        # принимать, иначе она проверяет не проводку, а свою же сигнатуру.
        lambda cfg, **kw: calls.append("ensure"),
    )
    monkeypatch.setattr(
        "recommended_mcp.runtime.daemon_autostart.shutdown_mcp_daemons",
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

        def _maybe_notice_profile_activation(self, *a, **k):   # cold-start notice §3.5 — no-op
            pass

        def run(self):
            return None

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)

    calls: list[str] = []
    monkeypatch.setattr(
        "recommended_mcp.runtime.daemon_autostart.ensure_mcp_daemons",
        lambda cfg, **kw: calls.append("ensure"),
    )
    monkeypatch.setattr(
        "recommended_mcp.runtime.daemon_autostart.shutdown_mcp_daemons",
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

        def _maybe_notice_profile_activation(self, *a, **k):   # cold-start notice §3.5 — no-op
            pass

        def run(self):
            return None

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)

    calls: list[str] = []
    monkeypatch.setattr(
        "recommended_mcp.runtime.daemon_autostart.ensure_mcp_daemons",
        lambda cfg, **kw: calls.append("ensure"),
    )
    monkeypatch.setattr(
        "recommended_mcp.runtime.daemon_autostart.shutdown_mcp_daemons",
        lambda: calls.append("shutdown"),
    )

    from gc_tui import main

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

    from gc_tui import app as tui_app
    from gc_tui.voice_runtime import autostart as autostart
    from gc_tui.voice_runtime import state as voice_state
    from gc_tui.voice_runtime.state import VoiceState

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
    monkeypatch.setattr("recommended_mcp.runtime.daemon_autostart.ensure_mcp_daemons", lambda cfg, **kw: None)
    monkeypatch.setattr("recommended_mcp.runtime.daemon_autostart.shutdown_mcp_daemons", lambda: None)

    state = VoiceState(version="v1", root="/data/voice/speaches", port=8735, model="m")
    monkeypatch.setattr(voice_state, "load_state", lambda: state)
    monkeypatch.setattr(autostart, "url_is_ours", lambda cfg, port: True)

    def _must_not_be_called(*a, **kw):
        raise AssertionError("main() не должен звать ensure_voice_service синхронно")
    monkeypatch.setattr(autostart, "ensure_voice_service", _must_not_be_called)

    from gc_tui import main

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

    from gc_tui import app as tui_app
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
    monkeypatch.setattr("recommended_mcp.runtime.daemon_autostart.ensure_mcp_daemons", lambda cfg, **kw: None)
    monkeypatch.setattr("recommended_mcp.runtime.daemon_autostart.shutdown_mcp_daemons", lambda: None)

    from gc_tui import main

    cfg_path = _cfg_for_main(tmp_path)
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "work.yml").write_text("slots: {}\n", encoding="utf-8")
    rc = main(argv=["--config", str(cfg_path), "--profile", "work"])
    assert rc == 0
    assert called.get("profile_override") == "work"


def test_main_emergency_exit_reuses_the_one_cleanup(monkeypatch, tmp_path):
    """Аварийный выход сторожа терминала и штатный `finally` зовут одну уборку
    main(), и она выполняется ровно раз — иначе демоны либо сиротеют, либо
    гасятся дважды. В неё же входят процессы индекса путей: при аварийном выходе
    git пикера тоже гасится. Порядок: git пикера, MCP-демоны, голос — MCP раньше
    голоса, чтобы зависший голосовой шаг не оставил демонов без SIGTERM. Сторож
    получает последний рубеж — добивание MCP-демонов этого запуска."""
    pytest.importorskip("textual")

    from gc_tui import app as tui_app
    from gc_tui.services import file_index, terminal_guard
    from gc_tui.voice_runtime import autostart

    guards: list = []

    class _SpyGuard(terminal_guard.TerminalGuard):
        def __init__(self, cleanup, **kw):
            super().__init__(cleanup, **kw)
            guards.append(self)

    class _FakeApp:
        def __init__(self, **kw):
            pass

        def set_config_path(self, path):   # как у настоящего TuiApp: путь + снимок mtime
            self._config_path = path

        def _maybe_notice_profile_activation(self, *a, **k):   # cold-start notice §3.5 — no-op
            pass

        def run(self):
            guards[0].run_cleanup()   # терминал умер посреди app.run() — аварийный путь

    monkeypatch.setattr(terminal_guard, "TerminalGuard", _SpyGuard)
    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)

    calls: list[str] = []
    monkeypatch.setattr(
        "recommended_mcp.runtime.daemon_autostart.ensure_mcp_daemons",
        lambda cfg, **kw: calls.append("ensure"),
    )
    monkeypatch.setattr(
        "recommended_mcp.runtime.daemon_autostart.shutdown_mcp_daemons",
        lambda: calls.append("mcp"),
    )

    def force_kill() -> None:
        calls.append("force")

    monkeypatch.setattr("recommended_mcp.runtime.daemon_autostart.kill_mcp_daemons_now", force_kill)
    monkeypatch.setattr(autostart, "release_voice_service", lambda: calls.append("voice"))
    monkeypatch.setattr(file_index, "kill_running_processes", lambda: calls.append("git"))

    from gc_tui import main

    cfg_path = _cfg_for_main(tmp_path)
    rc = main(argv=["--config", str(cfg_path)])
    assert rc == 0
    assert len(guards) == 1
    assert calls == ["ensure", "git", "mcp", "voice"]
    assert guards[0]._force_kill is force_kill


def test_main_without_auto_mcp_gives_guard_nothing_to_force_kill(monkeypatch, tmp_path):
    """Без автозапуска MCP-демонов нет — и добивать сторожу нечего."""
    pytest.importorskip("textual")

    from gc_tui import app as tui_app
    from gc_tui.services import terminal_guard

    guards: list = []

    class _SpyGuard(terminal_guard.TerminalGuard):
        def __init__(self, cleanup, **kw):
            super().__init__(cleanup, **kw)
            guards.append(self)

    class _FakeApp:
        def __init__(self, **kw):
            pass

        def set_config_path(self, path):   # как у настоящего TuiApp: путь + снимок mtime
            self._config_path = path

        def _maybe_notice_profile_activation(self, *a, **k):   # cold-start notice §3.5 — no-op
            pass

        def run(self):
            return None

    monkeypatch.setattr(terminal_guard, "TerminalGuard", _SpyGuard)
    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)

    from gc_tui import main

    rc = main(argv=["--config", str(_cfg_for_main(tmp_path)), "--no-auto-mcp"])
    assert rc == 0
    assert guards[0]._force_kill is None


@pytest.mark.parametrize("run_fails", [False, True])
def test_main_kills_file_index_processes_on_exit(monkeypatch, tmp_path, run_fails):
    """Выход из TUI гасит процессы индекса путей (git ls-files) — и когда app.run()
    упал: поток сборки — демон, а сам git пережил бы программу."""
    pytest.importorskip("textual")

    from gc_tui import app as tui_app
    from gc_tui.services import file_index

    class _FakeApp:
        def __init__(self, **kw):
            pass

        def set_config_path(self, path):   # как у настоящего TuiApp: путь + снимок mtime
            self._config_path = path

        def _maybe_notice_profile_activation(self, *a, **k):   # cold-start notice §3.5 — no-op
            pass

        def run(self):
            if run_fails:
                raise RuntimeError("boom")

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)
    monkeypatch.setattr("recommended_mcp.runtime.daemon_autostart.ensure_mcp_daemons",
                        lambda cfg, **kw: None)
    monkeypatch.setattr("recommended_mcp.runtime.daemon_autostart.shutdown_mcp_daemons",
                        lambda: None)
    killed = []
    monkeypatch.setattr(file_index, "kill_running_processes", lambda: killed.append(True))

    from gc_tui import main

    cfg_path = _cfg_for_main(tmp_path)
    if run_fails:
        with pytest.raises(RuntimeError, match="boom"):
            main(argv=["--config", str(cfg_path)])
    else:
        assert main(argv=["--config", str(cfg_path)]) == 0
    assert killed == [True]
