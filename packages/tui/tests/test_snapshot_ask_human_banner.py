"""ask_human banner — каноничный вид (§3.4)."""

from gc_testkit.tui_support import tui_settings
import pytest

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")

from gc_testkit.tui_fixtures.stub_executor import StubExecutor


class _StubLiveRunner:
    """Минимальный runner-stub, который представляется живым, чтобы
    Fix D recovery-блок в attach_active_task НЕ снял banner."""

    def is_running(self) -> bool:
        return True


def test_ask_human_banner_snapshot(snap_compare, tmp_path, monkeypatch):
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen

    # cwd-чип в футере фиксируем — иначе снапшот стал бы машинно-зависимым
    # (футер теперь показывает путь проекта, а не отладочный плейсхолдер).
    monkeypatch.setattr(
        TraceScreen, "_current_cwd", lambda self: "/work/demo-project"
    )

    tid = "ah_snap"
    fake = StubExecutor(
        threads_dir=str(tmp_path), system="cli", scenario="ask_human_flow"
    )
    fake.run(["используй скилл skill-creator"], thread_id=tid)

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {
                "agent.model": "CodeAgent",
                "backend.api_mode": "chat_completions",
            }
        ),
    )
    app._executor_factory = lambda: fake
    # Fix D: без живого runner-а recovery-блок снял бы banner. Здесь мы
    # фиксируем именно каноничный визуал banner-а → подсовываем live-stub.
    app.active_runner = _StubLiveRunner()

    async def setup(pilot):
        await pilot.pause()
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.5)

    assert snap_compare(app, terminal_size=(120, 32), run_before=setup)
