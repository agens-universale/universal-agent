"""Input-оверлей (дропдаун) помещается на экран при длинном трейсе.

Раскрывается вниз от ввода; при заполненном экране #trace ужимается, а высота
оверлея ограничена местом под вводом → футер и сам оверлей остаются на экране
(не клип). На закрытии #trace растёт обратно. Высокий оверлей моделируем Static'ом
(как длинный список skill/команд) — проверяем саму математику _mount_input_overlay."""
import json
from pathlib import Path

import pytest


def _write_task(tmp_path, tid):
    meta = {"thread_id": tid, "status": "running",
            "updated_at": "2026-06-15T12:00:00+00:00", "messages_count": 0}
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(json.dumps(meta), encoding="utf-8")
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


def _app(tmp_path):
    from tui.app import TuiApp
    return TuiApp(threads_dir=str(tmp_path), system="cli",
                  config={"model": "M", "backend_api_mode": "chat_completions"})


async def _tall(pilot, tid):
    scr = pilot.app.screen
    scr.attach_active_task(tid)
    big = "\n".join(f"строка {i}" for i in range(80))   # трейс на весь экран
    scr._render_record({"payload": {"id": "a1", "role": "assistant",
                                    "content": big}}, live=True)
    await pilot.pause(0.4)
    return scr


def _tall_overlay():
    from textual.widgets import Static
    # 40 строк — заведомо выше экрана (как длинный список skill/команд)
    return Static("\n".join(f"пункт {i}" for i in range(40)), classes="palette")


@pytest.mark.asyncio
async def test_overlay_fits_when_trace_tall(tmp_path):
    pytest.importorskip("textual")
    tid = "ov1"; _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = await _tall(pilot, tid)
        full_max = scr._trace_max_height          # #trace на весь экран до оверлея
        overlay = _tall_overlay()
        scr._mount_input_overlay(overlay)
        await pilot.pause(0.4)
        footer = pilot.app.query_one("#footer")
        assert footer.region.bottom <= 24         # футер на экране (не выдавлен)
        assert overlay.region.bottom <= 24        # оверлей на экране (не клип за низ)
        assert scr._trace_max_height < full_max   # #trace ужался под оверлей


@pytest.mark.asyncio
async def test_trace_regrows_after_overlay_close(tmp_path):
    pytest.importorskip("textual")
    tid = "ov2"; _write_task(tmp_path, tid)
    async with _app(tmp_path).run_test(size=(80, 24)) as pilot:
        scr = await _tall(pilot, tid)
        full_max = scr._trace_max_height
        overlay = _tall_overlay()
        scr._mount_input_overlay(overlay)
        await pilot.pause(0.4)
        assert scr._trace_max_height < full_max   # ужался
        overlay.remove()
        await pilot.pause(0.4)
        assert scr._trace_max_height == full_max   # вырос обратно на весь экран
