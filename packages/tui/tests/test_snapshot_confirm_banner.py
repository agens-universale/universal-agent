"""confirm banner — каноничный вид."""
import pytest

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")


def test_confirm_banner_snapshot(snap_compare, tmp_path, monkeypatch):
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen

    # cwd-чип в футере фиксируем — иначе снапшот стал бы машинно-зависимым.
    monkeypatch.setattr(TraceScreen, "_current_cwd", lambda self: "/work/demo-project")

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        config={"model": "CodeAgent", "backend_api_mode": "chat_completions"},
    )

    async def setup(pilot):
        await pilot.pause()
        pilot.app.screen.show_confirm_banner(
            "Command moderation",
            "Разрешить выполнение команды?",
            [
                ("Tool", "execute_command"),
                ("Команда", "rm -rf /tmp/build-cache"),
                ("Риск", "destructive_fs"),
            ],
        )
        await pilot.pause()

    assert snap_compare(app, terminal_size=(120, 32), run_before=setup)
