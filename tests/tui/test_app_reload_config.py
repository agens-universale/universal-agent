"""reload_config: перечитка конфига + сброс кэшированного runner (спека B, B3)."""
import pytest

pytest.importorskip("textual")

import tui.app as app_mod


class _FakeRunner:
    def __init__(self, running):
        self._running = running
    def is_running(self):
        return self._running


def _mk_app(tmp_path):
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("backend:\n  completions_url: \"http://x/v1\"\n", encoding="utf-8")
    app = app_mod.TuiApp(config={"completions_url": "old"})
    app._config_path = str(cfg_file)
    return app


def test_reload_config_rereads_and_resets_idle_runner(tmp_path):
    app = _mk_app(tmp_path)
    app.active_runner = _FakeRunner(running=False)
    app.reload_config()
    assert app._config["completions_url"] == "http://x/v1"
    assert app.active_runner is None                    # следующий submit соберёт свежий executor


def test_reload_config_defers_reset_while_running(tmp_path):
    app = _mk_app(tmp_path)
    runner = _FakeRunner(running=True)
    app.active_runner = runner
    app.reload_config()
    assert app.active_runner is runner                  # живой run не трогаем
    assert app._runner_stale is True
    runner._running = False
    got = app.get_runner()                              # run завершился → пересборка
    assert got is not runner
    assert app._runner_stale is False


def test_reload_config_idle_clears_stale_flag(tmp_path):
    """I1: утечка _runner_stale — idle-reload должен сбрасывать флаг."""
    app = _mk_app(tmp_path)
    # (а) runner жил и stale=True был выставлен на первом reload
    runner = _FakeRunner(running=True)
    app.active_runner = runner
    app.reload_config()
    assert app._runner_stale is True
    # (б) runner завершился, но второй reload зовут до get_runner (idle-ветка)
    runner._running = False
    app.reload_config()
    assert app.active_runner is None
    assert app._runner_stale is False
    # (в) get_runner создаёт новый runner, флаг по-прежнему False
    fake_runner = _FakeRunner(running=False)
    app._executor_factory = lambda: None
    # перехватываем AgentRunner: подменяем класс в tui.services.agent_runner
    import tui.services.agent_runner as runner_mod
    orig = runner_mod.AgentRunner
    runner_mod.AgentRunner = lambda **_kw: fake_runner
    try:
        got = app.get_runner()
    finally:
        runner_mod.AgentRunner = orig
    assert got is fake_runner
    assert app._runner_stale is False


@pytest.mark.asyncio
async def test_reload_config_preserves_runtime_fields(tmp_path, monkeypatch):
    """Hot-apply не теряет поля, вычисленные рантаймом TUI.

    launch_root ставит tui/__init__ (снапшот cwd на старте) — в файле конфига его нет.
    reload_config перечитывает файл, и без явного переноса поле исчезало:
      - /diff переставал работать («diff: launch_root не задан»);
      - периметр path_policy съезжал со снапшота на живой os.getcwd(), хотя агент мог
        сделать cd внутри сессии.
    """
    from tui.app import TuiApp
    import config as cfg_mod
    import thread_store as ts_mod

    on_disk = {"completions_url": "https://h/v1/chat/completions",
               "chat_completion_headers": {"Authorization": "Bearer sk"}, "model": "m"}
    monkeypatch.setattr(cfg_mod, "load_config", lambda p=None: dict(on_disk))  # файл: без launch_root
    monkeypatch.setattr(cfg_mod, "apply_cli_overrides", lambda d: None)
    monkeypatch.setattr(ts_mod, "resolve_threads_dir", lambda c: str(tmp_path / "t"))

    started = dict(on_disk)
    started["launch_root"] = "D:/proj/snapshot-at-start"     # как ставит tui/__init__
    started["console_trace"] = False

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config=started)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.reload_config()
        assert app._config["launch_root"] == "D:/proj/snapshot-at-start"
        assert app._config["console_trace"] is False
