"""Везде, где TUI снимает runner, он отдаёт логгер его прогона.

Утечку закрыл `AgentRunner.release`, но вызвать его обязан КАЖДЫЙ, кто снимает
runner: логгер прогона живёт в реестре `logging` до конца процесса и тянет за
собой экран, executor и кэши отрисовки. Выход из приложения проверяет
`test_runner_releases_run_logger`; здесь — три остальных места, каждое из
которых раньше просто обнуляло `active_runner`:

* смена конфига сессии (`invalidate_runner`);
* замена устаревшего runner'а (`get_runner`);
* остановка прогона по Esc / Ctrl+C (`TraceScreen._stop_and_replace_runner`).
"""

from gc_testkit.tui_support import tui_settings
from types import SimpleNamespace

import pytest


class _Runner:
    """Двойник AgentRunner: запоминает порядок вызовов."""

    def __init__(self, running: bool = False):
        self._running = running
        self.events: list[str] = []
        self._marked_stopped = False

    def is_running(self) -> bool:
        return self._running

    def stop(self) -> None:
        self.events.append("stop")

    def release(self) -> None:
        self.events.append("release")


@pytest.fixture
def app(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    cfg = {
        "agent.model": "M",
        "paths.agent_md_path": str(tmp_path / "agent.md"),
        "paths.threads_data_path": str(tmp_path / "threads"),
        "paths.logs_dump_dir": str(tmp_path / "logs"),
        "mcp.servers": {},
    }
    return TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )


def test_invalidate_runner_releases_the_idle_runner(app):
    runner = _Runner()
    app.active_runner = runner

    app.invalidate_runner()

    assert runner.events == ["release"]
    assert app.active_runner is None


def test_invalidate_runner_does_not_touch_a_live_run(app):
    """Живой прогон не трогаем: он ещё пишет в свой логгер."""
    runner = _Runner(running=True)
    app.active_runner = runner

    app.invalidate_runner()

    assert runner.events == []
    assert app.active_runner is runner  # снимут после завершения
    assert app._runner_stale is True


def test_get_runner_releases_the_stale_one(app):
    runner = _Runner()
    app.active_runner = runner
    app._runner_stale = True

    fresh = app.get_runner()

    assert runner.events == ["release"]
    assert fresh is not runner


def test_exit_stops_the_runner_before_releasing_it(app):
    """На выходе агент может ждать ответа человека — тогда процесс не завершится.

    Поток ожидания сидит в `queue.get()` внутри `asyncio.to_thread`, то есть в
    потоке пула `concurrent.futures`, а его интерпретатор ждёт на выходе. `/quit`
    зовёт `app.exit()` и прогон не останавливает, поэтому снять ожидание должен
    `on_unmount`: `stop()` кладёт в очередь пустую строку и гасит confirm.
    """
    runner = _Runner(running=True)
    app.active_runner = runner

    app.on_unmount()

    assert runner.events == ["stop", "release"], (
        f"порядок на выходе: {runner.events} — ожидание человека не снято"
    )


def test_stop_and_replace_releases_after_stop():
    """Esc / Ctrl+C: сначала кооперативный стоп, потом отдача логгера.

    Порядок существен: `release` на работающем воркере откладывает отдачу до
    выхода потока, и наоборот — отнимать хендлеры у пишущего прогона нельзя."""
    pytest.importorskip("textual")
    from gc_tui.screens.main import TraceScreen

    runner = _Runner(running=True)
    screen = SimpleNamespace(
        app=SimpleNamespace(active_runner=runner, last_stop_requested=False)
    )

    TraceScreen._stop_and_replace_runner(screen)

    assert runner.events == ["stop", "release"]
    assert runner._marked_stopped is True
    assert screen.app.active_runner is None
    assert screen.app.last_stop_requested is True
