"""Окно «ответ дан, прогон ещё жив» (soft-hide-while-running) показывает
«сверяюсь…» вместо пустоты — объясняет статус [running]."""

from gc_testkit.tui_support import tui_settings
import time

import pytest


def _write_task(tmp_path, tid):
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        f'{{"thread_id":"{tid}","status":"running","updated_at":"2026-06-23T12:00:00+00:00","messages_count":1}}',
        encoding="utf-8",
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


class _Runner:
    def is_running(self):
        return True

    def stop(self):
        pass


@pytest.mark.asyncio
async def test_running_after_answer_shows_processing(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    tid = "proc0000000000000000000000000001a"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        pilot.app.active_runner = _Runner()
        # ответ (agent) показан, ≥1.5с тишины, прогон ещё жив
        # Показанный ответ - видимое событие: взводим гейт «сверяюсь…».
        scr._last_event_kind = "agent"
        scr._saw_visible_event = True
        scr._last_event_at = time.monotonic() - 5
        scr._poll_active()
        await pilot.pause()
        ind = scr.query_one(ThinkingIndicator)
        assert "сверяюсь…" in str(ind._render_text().plain)


@pytest.mark.asyncio
async def test_continuation_start_says_thinking_then_processing(tmp_path):
    """Континуация: первое событие скрытое, тишина 3 с - «думаю»;
    после видимого ответа и тишины 2 с - «сверяюсь»."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    tid = "proc0000000000000000000000000002a"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        pilot.app.active_runner = _Runner()
        scr._render_record(
            {
                "payload": {
                    "id": "t1",
                    "role": "user",
                    "content": '<technical type="bounce">x</technical>',
                }
            }
        )
        scr._last_event_at = time.monotonic() - 3
        scr._poll_active()
        await pilot.pause()
        ind = scr.query_one(ThinkingIndicator)
        assert "сверяюсь…" not in str(ind._render_text().plain)
        scr._render_record(
            {"payload": {"id": "a1", "role": "assistant", "content": "ответ"}}
        )
        scr._last_event_at = time.monotonic() - 2
        scr._poll_active()
        await pilot.pause()
        ind = scr.query_one(ThinkingIndicator)
        assert "сверяюсь…" in str(ind._render_text().plain)
