"""«сверяюсь…» — только когда ядро подняло флаг судьи ответа
(meta.runtime.finish_gate_checking). Тишина после ответа или скрытой
техзаметки — обычная работа модели: «думаю…»."""

import json

from gc_testkit.tui_support import tui_settings
import pytest


def _write_task(tmp_path, tid, runtime=None):
    meta = {
        "thread_id": tid,
        "status": "running",
        "updated_at": "2026-06-23T12:00:00+00:00",
        "messages_count": 1,
    }
    if runtime:
        meta["runtime"] = runtime
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


class _Runner:
    def is_running(self):
        return True

    def stop(self):
        pass


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )


async def _indicator_text(pilot, tid):
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    scr = pilot.app.screen
    scr._poll_active()
    await pilot.pause()
    return str(scr.query_one(ThinkingIndicator)._render_text().plain)


@pytest.mark.asyncio
async def test_hidden_mode_notice_after_reply_says_thinking(tmp_path):
    """Реплика, следом скрытая техзаметка режима и тишина — идёт обычный
    вызов модели: «думаю…», не «сверяюсь…»."""
    tid = "proc0000000000000000000000000001a"
    _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        pilot.app.active_runner = _Runner()
        scr._render_record(
            {"payload": {"id": "u1", "role": "user", "content": "привет"}}
        )
        scr._render_record(
            {
                "payload": {
                    "id": "t1",
                    "role": "user",
                    "content": '<technical type="technical">режим</technical>',
                }
            }
        )
        await pilot.pause(2.0)
        text = await _indicator_text(pilot, tid)
        assert "сверяюсь…" not in text
        assert "думаю" in text


@pytest.mark.asyncio
async def test_judge_flag_from_core_says_checking(tmp_path):
    tid = "proc0000000000000000000000000002a"
    _write_task(tmp_path, tid, runtime={"finish_gate_checking": True})
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        pilot.app.active_runner = _Runner()
        assert "сверяюсь…" in await _indicator_text(pilot, tid)
