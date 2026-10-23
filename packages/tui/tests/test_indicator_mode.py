"""Выбор режима строки-индикатора в _poll_active.

Human-wait (ask/confirm) скрывает индикатор даже при err — иначе stale
last_runner_error от заменённого runner рисует ✗ Ошибка поверх баннера.
Краш текущего раннера (err) приоритетнее running → терминальный ✗ Ошибка сразу,
не дожидаясь reaping воркера / falling-edge."""

import pytest
from gc_tui.screens.main import select_indicator_mode as M


def test_human_wait_hides_even_with_err():
    assert (
        M(err="boom", is_running=True, ask_human=True, confirm=False) == "hide"
    )
    assert (
        M(err="boom", is_running=False, ask_human=False, confirm=True)
        == "hide"
    )


def test_err_takes_precedence_over_running():
    assert (
        M(err="boom", is_running=True, ask_human=False, confirm=False)
        == "failed"
    )
    assert (
        M(err="boom", is_running=False, ask_human=False, confirm=False)
        == "failed"
    )


def test_active_when_running_without_human_wait():
    assert (
        M(err=None, is_running=True, ask_human=False, confirm=False)
        == "active"
    )


def test_hide_on_human_wait():
    assert (
        M(err=None, is_running=True, ask_human=True, confirm=False) == "hide"
    )
    assert (
        M(err=None, is_running=False, ask_human=False, confirm=True) == "hide"
    )


def test_terminal_when_idle():
    assert (
        M(err=None, is_running=False, ask_human=False, confirm=False)
        == "terminal"
    )


# --- S10: ошибка хода и быстрая новая отправка (гарантия 1ac65d4d9) ---------


@pytest.mark.asyncio
async def test_error_then_quick_send_settles_above_reply(tmp_path):
    """Быстрое «продолжай» после ошибки (поток раннера ещё жив): строка
    «✕ Ошибка» встала сразу и не теряется — осевшая, над новой репликой."""
    import json

    from gc_testkit.tui_support import tui_settings
    from gc_tui.app import TuiApp
    from gc_tui.widgets.turn_line import TurnLine
    from gc_tui.widgets.user_reply import UserReplyContainer

    tid = "qerr000000000000000000000000001a"
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system_id": "cli"}), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")

    class _Run:
        _thread_kind = "run"
        _last_target = (tid, "cli")
        run_seq = 1

        def __init__(self):
            self.running = True

        def is_running(self):
            return self.running

        def turn_effective_start(self):
            return None

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid, is_resume=True)
        pilot.app.active_runner = _Run()
        scr._prepare_for_new_run()
        scr._poll_tick()
        pilot.app.last_runner_error = "ReadTimeout: сервер не ответил"
        scr._poll_tick()  # ошибка — строка в том же тике

        scr._mount_user_reply("продолжай", state="pending", client_id="c2")
        scr._prepare_for_new_run()
        await pilot.pause()
        kids = list(scr.query_one("#trace").children)
        (line,) = [w for w in kids if isinstance(w, TurnLine)]
        (reply,) = [w for w in kids if isinstance(w, UserReplyContainer)]
        assert line.word == "failed"
        assert not line.is_fresh
        assert line.display
        assert kids.index(line) < kids.index(reply)


@pytest.mark.asyncio
async def test_queued_run_after_settled_error_not_closed_by_stale_error(
    tmp_path,
):
    """Ошибка закрыла прогон при живом потоке; отправка из поля стартует
    следующий прогон того же раннера без сброса флагов. Ошибка прошлого
    прогона новый не закрывает: бар стоит, исход второй раз не пишется."""
    import json

    from gc_testkit.tui_support import tui_settings
    from gc_tui.app import TuiApp

    tid = "qerr000000000000000000000000002b"
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system_id": "cli"}), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")

    class _Run:
        _thread_kind = "run"
        _last_target = (tid, "cli")
        run_seq = 1

        def is_running(self):
            return True

        def turn_effective_start(self):
            return None

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid, is_resume=True)
        runner = _Run()
        pilot.app.active_runner = runner
        scr._prepare_for_new_run()
        scr._poll_tick()
        pilot.app.last_runner_error = "ReadTimeout: сервер не ответил"
        scr._poll_tick()  # ошибка закрыла прогон 1

        finished = []
        orig = scr._on_run_finished
        scr._on_run_finished = lambda r, k: (finished.append(k), orig(r, k))
        runner.run_seq = 2  # add_message: прогон 2 без _reset_run_flags
        scr._poll_tick()
        scr._poll_tick()
        await pilot.pause()

        assert finished == []
        bar = scr.query_one("#indicator-bar")
        assert bar.has_class("-turn")
