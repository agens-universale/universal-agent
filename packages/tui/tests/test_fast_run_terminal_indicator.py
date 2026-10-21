"""Строка-итог хода, когда прогон уложился между тиками опроса.

Индикатор создаёт только тик опроса, заставший прогон бегущим. Быстрый ход
(завершился раньше первого тика) индикатора не имел — строка «✓ Выполнил»
должна появиться и без него, как при переоткрытии сессии.
"""

import json

import pytest
from gc_testkit.tui_support import tui_settings

TID = "fastrun0000000000000000000000001"


class _Runner:
    _thread_kind = "run"
    _stop_requested = False

    def __init__(self, target, running):
        self._last_target = target
        self.running = running

    def is_running(self):
        return self.running

    def stop(self):
        self.running = False


def _app(tmp_path):
    from gc_tui.app import TuiApp

    meta = {
        "thread_id": TID,
        "system_id": "cli",
        "tui": {"status": "completed"},
    }
    (tmp_path / f"meta_cli_{TID}.json").write_text(json.dumps(meta), "utf-8")
    (tmp_path / f"data_cli_{TID}.jsonl").write_text("", encoding="utf-8")
    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


async def _send(pilot, *, seen_running):
    """Отправка как в on_input_submitted; тик опроса либо застаёт прогон
    бегущим, либо приходит уже после его конца."""
    scr = pilot.app.screen
    scr.attach_active_task(TID)
    await pilot.pause()
    scr._prepare_for_new_run()
    scr._reset_thinking_timer()
    runner = _Runner((TID, pilot.app._system), running=seen_running)
    pilot.app.active_runner = runner
    if seen_running:
        scr._poll_active()
    return scr, runner


def _indicators(pilot):
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    return list(pilot.app.screen.query(ThinkingIndicator))


@pytest.mark.asyncio
@pytest.mark.parametrize("seen_running", [True, False])
async def test_finished_run_shows_done_line(tmp_path, seen_running):
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _send(pilot, seen_running=seen_running)
        runner.running = False
        scr._poll_active()
        scr._poll_active()
        await pilot.pause()

        inds = _indicators(pilot)
        assert [i._state for i in inds] == ["done"]
        assert inds[0]._render_text().plain.startswith("✓ Выполнил")


@pytest.mark.asyncio
async def test_undo_cancel_does_not_resurrect_line(tmp_path):
    """Отмена без ответа агента — «undo»: строку-итог не возвращаем."""
    async with _app(tmp_path).run_test() as pilot:
        scr, _ = await _send(pilot, seen_running=True)
        scr._cancel_with_undo()
        scr._poll_active()
        scr._poll_active()
        await pilot.pause()

        assert _indicators(pilot) == []


def _edit(tmp_path, turn):
    """Запись журнала правок хода ``turn``: один файл, +1 −1."""
    from gc_core.agent_edits.journal import EditRecord, EditsJournal
    from gc_core.thread_store import edits_journal_path

    path = edits_journal_path(str(tmp_path), "cli", TID)
    EditsJournal(path).append(
        EditRecord(
            path="a.py",
            turn_id=turn,
            ts=1.0,
            call_id=f"c_{turn}",
            op="edit",
            hunks="@@ -1 +1 @@\n-a\n+b\n",
            add=1,
            del_=1,
            before_sha256="x",
            after_sha256="y",
            flags=[],
        )
    )


async def _fast_turn(pilot, tmp_path, turn=None):
    """Ход, уложившийся между тиками; ``turn`` — ход журнала с правкой."""
    scr, runner = await _send(pilot, seen_running=False)
    if turn is not None:
        _edit(tmp_path, turn)
    runner.running = False
    scr._poll_active()
    await pilot.pause()
    return scr


@pytest.mark.asyncio
async def test_fast_run_with_edits_shows_edit_counter(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        await _fast_turn(pilot, tmp_path, turn="T1")

        inds = _indicators(pilot)
        assert [i._state for i in inds] == ["done"]
        assert "изменено 1 файл" in inds[0]._render_text().plain


@pytest.mark.asyncio
async def test_next_turn_summary_skips_fast_run_edits(tmp_path):
    """Быстрый ход закрыт целиком: граница правок следующего хода — новая."""
    async with _app(tmp_path).run_test() as pilot:
        await _fast_turn(pilot, tmp_path, turn="T1")
        scr, runner = await _send(pilot, seen_running=True)
        runner.running = False
        scr._poll_active()
        await pilot.pause()

        inds = _indicators(pilot)
        assert [i._state for i in inds] == ["done"]
        assert "изменено" not in inds[0]._render_text().plain


@pytest.mark.asyncio
async def test_fast_run_drops_skills_cache(tmp_path):
    """Быстрый ход мог создать скилл: кэш скиллов перечитается."""
    async with _app(tmp_path).run_test() as pilot:
        pilot.app.screen._skills_cache = [{"id": "old"}]
        scr = await _fast_turn(pilot, tmp_path)

        assert scr._skills_cache is None


@pytest.mark.asyncio
async def test_fast_run_finish_keeps_done_state(tmp_path):
    """Конец хода и turn_finish в одном тике: строка-итог не мигает
    «завершаю…»."""
    from gc_core.thread_store import append_new_thread_messages

    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _send(pilot, seen_running=False)
        append_new_thread_messages(
            str(tmp_path),
            "cli",
            TID,
            [
                {"id": "u1", "role": "user", "content": "q"},
                {
                    "id": "a1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Ответ."}],
                },
                {
                    "id": "f1",
                    "type": "function_call",
                    "name": "turn_finish",
                    "call_id": "c1",
                    "arguments": json.dumps({"task_report": "Отчёт."}),
                },
                {
                    "id": "fo1",
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": "ok",
                },
            ],
            set(),
            start_seq=0,
        )
        # Состояние сразу после тика: следующий тик таймера экрана вернул
        # бы «done» и скрыл мигание.
        scr._poll_active()

        assert scr._thinking._state == "done"
