"""Открытие из списка сессий в фоне (спека шага 2 окна ленты, §3.3, §5.7):
ждём там, где нажали — список с индикатором у карточки; экран сессии и
превью открываются готовыми; отмена — всем, что меняет выбор или уводит
из списка; вопрос о стопе хода — сразу по Enter, стоп — в момент
перехода."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from gc_testkit.develop_files import append_new_thread_messages
from gc_testkit.tui_support import wait_until
from gc_tui.screens.confirm import ConfirmScreen
from gc_tui.screens.preview import PreviewScreen
from gc_tui.screens.sessions import SessionsScreen
from gc_tui.services import history_loader
from gc_tui.services.session_lock import BUSY_MESSAGE, try_acquire
from open_support import (
    A,
    B,
    C,
    Gate,
    card_text,
    join_loaders,
    make_app,
    open_list,
    run_in_a,
    settle,
    user_texts,
    write_threads,
)

LABEL = "открываю сессию"


@pytest.fixture
def gate(monkeypatch):
    return Gate(monkeypatch)


def _dialog(pilot):
    found = [s for s in pilot.app.screen_stack if isinstance(s, ConfirmScreen)]
    return found[0] if found else None


@pytest.mark.asyncio
async def test_enter_shows_indicator_after_delay_only(tmp_path, gate):
    write_threads(tmp_path)
    async with make_app(tmp_path).run_test() as pilot:
        main = pilot.app.screen
        main.attach_active_task(A, is_resume=True)
        lst = await open_list(pilot, tmp_path)
        gate.close()
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert LABEL not in card_text(lst)
        await pilot.pause(0.5)
        assert f"• {LABEL}" in card_text(lst)
        assert main._active_task_id == A
        gate.open()
        await settle(pilot)
        assert pilot.app.screen is main
        assert main._active_task_id == B
        assert "реплика B" in user_texts(main)


@pytest.mark.asyncio
async def test_fast_open_shows_nothing(tmp_path):
    write_threads(tmp_path)
    async with make_app(tmp_path).run_test() as pilot:
        main = pilot.app.screen
        lst = await open_list(pilot, tmp_path)
        painted = []
        real_card = lst._card_text
        lst._card_text = lambda task, **kw: (
            painted.append(kw.get("opening")) or real_card(task, **kw)
        )
        await pilot.press("enter")
        await settle(pilot)
        assert main._active_task_id == B
        assert [p for p in painted if p is not None] == []


@pytest.mark.asyncio
async def test_esc_cancels_and_stays(tmp_path, gate):
    write_threads(tmp_path)
    async with make_app(tmp_path).run_test() as pilot:
        main = pilot.app.screen
        main.attach_active_task(A, is_resume=True)
        lst = await open_list(pilot, tmp_path)
        gate.close()
        await pilot.press("enter")
        await pilot.pause(0.7)
        assert LABEL in card_text(lst)
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen is lst
        assert LABEL not in card_text(lst)
        gate.open()
        join_loaders()
        await pilot.pause()
        assert pilot.app.screen is lst
        assert main._active_task_id == A
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen is main


async def _type_in_filter(pilot, lst):
    from textual.widgets import Input

    lst.query_one("#sessions-filter", Input).value = "реплика"
    await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key", ["down", "up", "filter", "ctrl+p", "delete", "ctrl+r"]
)
async def test_arrows_filter_ctrlp_del_rename_cancel_first(
    tmp_path, gate, key
):
    write_threads(tmp_path)
    async with make_app(tmp_path).run_test() as pilot:
        main = pilot.app.screen
        main.attach_active_task(A, is_resume=True)
        lst = await open_list(pilot, tmp_path)
        gate.close()
        await pilot.press("enter")
        await pilot.pause()
        assert lst._opening == (B, "session")
        if key == "filter":
            await _type_in_filter(pilot, lst)
        else:
            await pilot.press(key)
            await pilot.pause()
        assert lst._opening != (B, "session")
        gate.open()
        await settle(pilot)
        assert main._active_task_id == A


@pytest.mark.asyncio
async def test_repeat_enter_noop(tmp_path, gate, monkeypatch):
    write_threads(tmp_path)
    async with make_app(tmp_path).run_test() as pilot:
        main = pilot.app.screen
        lst = await open_list(pilot, tmp_path)
        starts = []
        real = lst._loader.start
        monkeypatch.setattr(
            lst._loader,
            "start",
            lambda *a, **kw: starts.append(a[0]) or real(*a, **kw),
        )
        gate.close()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert starts == [B]
        gate.open()
        await settle(pilot)
        assert main._active_task_id == B
        assert len(gate.models) == 1


@pytest.mark.asyncio
async def test_run_confirm_before_load_and_stop_on_switch(tmp_path, gate):
    write_threads(tmp_path)
    async with make_app(tmp_path).run_test() as pilot:
        main, runner = await run_in_a(pilot)
        lst = await open_list(pilot, tmp_path)
        gate.close()
        await pilot.press("enter")
        await pilot.pause()
        # Вопрос — сразу, поверх списка; загрузки ещё нет.
        assert _dialog(pilot) is not None
        assert pilot.app.screen_stack[-2] is lst
        assert gate.inputs == []
        await pilot.press("y")
        await pilot.pause()
        # Загрузка идёт, ход работает.
        assert lst._loader.busy
        assert runner.stops == 0
        assert pilot.app.screen is lst
        gate.open()
        await settle(pilot)
        assert runner.stops == 1
        assert main._active_task_id == B
        assert pilot.app.screen is main


@pytest.mark.asyncio
async def test_run_confirm_stay_loads_nothing(tmp_path, gate):
    write_threads(tmp_path)
    async with make_app(tmp_path).run_test() as pilot:
        main, runner = await run_in_a(pilot)
        lst = await open_list(pilot, tmp_path)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert pilot.app.screen is lst
        assert lst._opening is None
        assert gate.inputs == []
        assert runner.stops == 0


@pytest.mark.asyncio
async def test_cancel_does_not_stop_run(tmp_path, gate):
    write_threads(tmp_path)
    async with make_app(tmp_path).run_test() as pilot:
        main, runner = await run_in_a(pilot)
        lst = await open_list(pilot, tmp_path)
        gate.close()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        gate.open()
        join_loaders()
        await pilot.pause()
        assert pilot.app.screen is lst
        assert runner.stops == 0
        assert runner.is_running()
        assert main._active_task_id == A


@pytest.mark.asyncio
async def test_busy_during_load_keeps_list_and_run(tmp_path, gate):
    """Сессию заняли за время загрузки: замок перепроверен до стопа хода
    и закрытия списка — список остаётся, «занято», ход идёт."""
    write_threads(tmp_path)
    async with make_app(tmp_path).run_test() as pilot:
        notes = []
        pilot.app.notify = lambda msg, **kw: notes.append(msg)
        main, runner = await run_in_a(pilot)
        lst = await open_list(pilot, tmp_path)
        gate.close()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert try_acquire(str(tmp_path), "cli", B, "other-tui")
        gate.open()
        await settle(pilot)
        assert BUSY_MESSAGE in notes
        assert pilot.app.screen is lst
        assert lst._opening is None
        assert runner.stops == 0
        assert main._active_task_id == A


@pytest.mark.asyncio
async def test_ctrl_p_opens_preview_ready(tmp_path, gate, monkeypatch):
    from gc_tui.services.stores.file_store import FileTaskStore

    write_threads(tmp_path)
    reads = []
    real = FileTaskStore.read_history
    monkeypatch.setattr(
        FileTaskStore,
        "read_history",
        lambda self, tid, *a: reads.append(tid) or real(self, tid, *a),
    )
    async with make_app(tmp_path).run_test() as pilot:
        lst = await open_list(pilot, tmp_path)
        gate.close()
        await pilot.press("ctrl+p")
        await pilot.pause(0.7)
        assert pilot.app.screen is lst
        assert "• открываю превью" in card_text(lst)
        gate.open()
        await settle(pilot)
        preview = pilot.app.screen
        assert isinstance(preview, PreviewScreen)
        assert reads == [B]
        assert preview._model is gate.models[0]
        assert preview._cursor is not None
        assert "реплика B" in user_texts(preview)
        assert LABEL not in card_text(lst)


@pytest.mark.asyncio
async def test_records_between_snapshot_and_open_once(tmp_path, monkeypatch):
    """Записи, легшие между фоновым снимком и открытием, приходят хвостом
    от курсора снимка — ровно один раз."""
    write_threads(tmp_path)
    real = history_loader.build_history

    def build(segments, **kw):
        append_new_thread_messages(
            str(tmp_path),
            "cli",
            B,
            [{"id": "u_late", "role": "user", "content": "поздняя реплика"}],
            set(),
            start_seq=1,
        )
        return real(segments, **kw)

    monkeypatch.setattr(history_loader, "build_history", build)
    async with make_app(tmp_path).run_test() as pilot:
        main = pilot.app.screen
        await open_list(pilot, tmp_path)
        await pilot.press("enter")
        await settle(pilot)
        assert main._active_task_id == B
        main._poll_trace_tail()
        await pilot.pause()
        main._poll_trace_tail()
        await pilot.pause()
        assert user_texts(main).count("поздняя реплика") == 1
        assert user_texts(main).count("реплика B") == 1


@pytest.mark.asyncio
async def test_result_after_switch_dropped(tmp_path, gate, monkeypatch):
    """Результат загрузки B готов, но выбор уже ушёл на C: B
    выбрасывается, открывается только C."""
    write_threads(tmp_path)
    async with make_app(tmp_path).run_test() as pilot:
        main = pilot.app.screen
        switched = []
        real = main.switch_to_history
        monkeypatch.setattr(
            main,
            "switch_to_history",
            lambda tid, *a: switched.append(tid) or real(tid, *a),
        )
        lst = await open_list(pilot, tmp_path)
        gate.after = True
        gate.close()
        await pilot.press("enter")
        await wait_until(pilot, lambda: gate.models, "модель B построена")
        await pilot.press("down")
        await pilot.pause()
        gate.after = False
        await pilot.press("enter")
        gate.open()
        await settle(pilot)
        assert switched == [C]
        assert main._active_task_id == C
        assert "реплика B" not in user_texts(main)
        assert lst.is_attached is False


@pytest.mark.asyncio
async def test_new_session_and_first_send_thread_stay_sync(
    tmp_path, monkeypatch
):
    """Страж: новая сессия и тред под первую отправку открываются
    синхронно, без загрузчика и индикатора."""
    write_threads(tmp_path)

    def no_loader(*_a, **_kw):
        raise AssertionError("загрузчик не нужен")

    monkeypatch.setattr(history_loader.HistoryLoader, "start", no_loader)
    async with make_app(tmp_path).run_test() as pilot:
        main = pilot.app.screen
        main.attach_active_task(A, is_resume=True)
        main._cmd_new()
        assert main._active_task_id is None
        assert main.attach_active_task(C) is True
        assert main._active_task_id == C
        assert "реплика C" in user_texts(main)
        assert not [
            s for s in pilot.app.screen_stack if isinstance(s, SessionsScreen)
        ]
