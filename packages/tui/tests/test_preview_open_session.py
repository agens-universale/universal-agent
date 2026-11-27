"""Превью → сессия в фоне (спека шага 2 окна ленты, §3.3, путь 3; §5.7):
превью ждёт с индикатором на месте строки подсказок; Esc отменяет и
оставляет в превью, прокрутка не отменяет; модель экрана сессии
строится из прочитанного превью без повторного чтения, со входами
целевой сессии."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from gc_tui.screens import preview as preview_mod
from gc_tui.screens.preview import _FOOTER_HINTS, PreviewScreen
from gc_tui.screens.sessions import SessionsScreen
from gc_tui.services.history_model import HistoryInputs
from gc_tui.services.stores.file_store import FileTaskStore
from open_support import (
    A,
    B,
    Gate,
    join_loaders,
    make_app,
    open_list,
    run_in_a,
    settle,
    user_texts,
    write_threads,
)
from textual.widgets import Static

LABEL = "открываю сессию"


@pytest.fixture
def gate(monkeypatch):
    return Gate(monkeypatch)


def _footer(preview) -> str:
    return str(preview.query_one("#preview-footer", Static).render())


async def _open_preview(pilot, tmp_path):
    main = pilot.app.screen
    main.attach_active_task(A, is_resume=True)
    lst = await open_list(pilot, tmp_path)
    await pilot.press("ctrl+p")
    await settle(pilot)
    preview = pilot.app.screen
    assert isinstance(preview, PreviewScreen)
    return main, lst, preview


@pytest.mark.asyncio
async def test_enter_indicator_in_hints_row(tmp_path, gate):
    write_threads(tmp_path)
    async with make_app(tmp_path).run_test() as pilot:
        main, lst, preview = await _open_preview(pilot, tmp_path)
        gate.close()
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert _footer(preview) == _FOOTER_HINTS
        await pilot.pause(0.5)
        assert _footer(preview).startswith(f"• {LABEL}")
        assert pilot.app.screen is preview
        gate.open()
        await settle(pilot)
        assert pilot.app.screen is main
        assert main._active_task_id == B
        assert not any(
            isinstance(s, (SessionsScreen, PreviewScreen))
            for s in pilot.app.screen_stack
        )


@pytest.mark.asyncio
async def test_load_failure_stays_in_preview(tmp_path, monkeypatch):
    """Сбой построения модели: приложение не падает — подсказки на
    месте, превью остаётся, причина в уведомлении."""
    import threading

    from gc_tui.services import history_loader

    write_threads(tmp_path)
    release = threading.Event()

    def broken(_segments, **_kw):
        release.wait(10)
        raise ValueError("битая запись")

    async with make_app(tmp_path).run_test() as pilot:
        main, _lst, preview = await _open_preview(pilot, tmp_path)
        monkeypatch.setattr(history_loader, "build_history", broken)
        await pilot.press("enter")
        await pilot.pause(0.7)
        assert _footer(preview).startswith(f"• {LABEL}")
        release.set()
        await settle(pilot)
        assert pilot.app.screen is preview
        assert _footer(preview) == _FOOTER_HINTS
        assert main._active_task_id == A
        assert any(
            "битая запись" in n.message for n in pilot.app._notifications
        )


@pytest.mark.asyncio
async def test_esc_cancels_stays_in_preview(tmp_path, gate):
    write_threads(tmp_path)
    async with make_app(tmp_path).run_test() as pilot:
        main, lst, preview = await _open_preview(pilot, tmp_path)
        gate.close()
        await pilot.press("enter")
        await pilot.pause(0.7)
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen is preview
        assert _footer(preview) == _FOOTER_HINTS
        gate.open()
        join_loaders()
        await pilot.pause()
        assert pilot.app.screen is preview
        assert main._active_task_id == A
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen is lst


@pytest.mark.asyncio
async def test_scroll_does_not_cancel(tmp_path, gate):
    write_threads(tmp_path)
    async with make_app(tmp_path).run_test() as pilot:
        main, lst, preview = await _open_preview(pilot, tmp_path)
        gate.close()
        await pilot.press("enter")
        await pilot.pause()
        for key in ("up", "pageup", "down", "end"):
            await pilot.press(key)
            await pilot.pause()
        assert preview._loader.busy
        gate.open()
        await settle(pilot)
        assert main._active_task_id == B


@pytest.mark.asyncio
async def test_repeat_enter_noop(tmp_path, gate):
    write_threads(tmp_path)
    async with make_app(tmp_path).run_test() as pilot:
        main, lst, preview = await _open_preview(pilot, tmp_path)
        gate.close()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        gate.open()
        await settle(pilot)
        # путь 2 и один путь 3
        assert len(gate.models) == 2
        assert main._active_task_id == B


@pytest.mark.asyncio
async def test_handover_no_reread_with_session_inputs(
    tmp_path, gate, monkeypatch
):
    write_threads(tmp_path)
    reads = []
    real_read = FileTaskStore.read_history
    monkeypatch.setattr(
        FileTaskStore,
        "read_history",
        lambda self, tid, *a: reads.append(tid) or real_read(self, tid, *a),
    )
    session_inputs = HistoryInputs(root="/сессия", injected_fields=("why",))
    asked = []
    monkeypatch.setattr(
        preview_mod,
        "history_inputs_for",
        lambda app, tid: asked.append(tid) or session_inputs,
    )
    async with make_app(tmp_path).run_test() as pilot:
        main, lst, preview = await _open_preview(pilot, tmp_path)
        segments = preview._segments
        assert reads == [A, B]  # открытие A и путь 2
        await pilot.press("enter")
        await settle(pilot)
        assert reads == [A, B]  # переход — без повторного чтения
        assert asked == [B]
        assert gate.inputs[-1] == session_inputs
        assert gate.inputs[0] != session_inputs
        assert main._feed_history is gate.models[-1]
        assert main._active_task_id == B
        assert "реплика B" in user_texts(main)
        assert segments


@pytest.mark.asyncio
async def test_run_confirm_over_preview_then_stop_on_switch(tmp_path, gate):
    write_threads(tmp_path)
    async with make_app(tmp_path).run_test() as pilot:
        main, runner = await run_in_a(pilot)
        await open_list(pilot, tmp_path)
        await pilot.press("ctrl+p")
        await settle(pilot)
        preview = pilot.app.screen
        gate.close()
        await pilot.press("enter")
        await pilot.pause()
        assert pilot.app.screen_stack[-2] is preview
        await pilot.press("y")
        await pilot.pause()
        assert runner.stops == 0
        gate.open()
        await settle(pilot)
        assert runner.stops == 1
        assert main._active_task_id == B


@pytest.mark.asyncio
async def test_busy_target_keeps_preview(tmp_path, gate):
    from gc_tui.services.session_lock import BUSY_MESSAGE, try_acquire

    write_threads(tmp_path)
    async with make_app(tmp_path).run_test() as pilot:
        notes = []
        pilot.app.notify = lambda msg, **kw: notes.append(msg)
        main, lst, preview = await _open_preview(pilot, tmp_path)
        gate.close()
        await pilot.press("enter")
        await pilot.pause()
        assert try_acquire(str(tmp_path), "cli", B, "other-tui")
        gate.open()
        await settle(pilot)
        assert BUSY_MESSAGE in notes
        assert pilot.app.screen is preview
        assert main._active_task_id == A
