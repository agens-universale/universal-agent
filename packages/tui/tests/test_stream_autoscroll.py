"""Стрим-ответ: stick-to-bottom при росте StreamBlock.

Когда блок растёт по мере стрима — следуем за низом, НО только если юзер уже
внизу; если он отскроллил вверх читать — не дёргаем его вид. Тесты
детерминированы: контролируем `_trace_is_at_bottom` и шпионим `scroll_end`
(без зависимости от геометрии вьюпорта)."""

import json
from pathlib import Path

import pytest


def _write_task(tmp_path, tid):
    meta = {
        "thread_id": tid,
        "status": "running",
        "updated_at": "2026-06-15T12:00:00+00:00",
        "messages_count": 0,
    }
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


class _FakeRunner:
    def is_running(self) -> bool:
        return True

    def stop(self) -> None:
        pass


@pytest.mark.asyncio
async def test_stream_autoscrolls_on_growth_when_at_bottom(
    tmp_path, monkeypatch
):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.containers import VerticalScroll

    tid = "scroll1"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        config={
            "model": "M",
            "backend_api_mode": "chat_completions",
            "tui_stream_display": "answer",
        },
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        pilot.app.active_runner = _FakeRunner()
        monkeypatch.setattr(scr, "_trace_is_at_bottom", lambda: True)
        trace = pilot.app.query_one("#trace", VerticalScroll)
        calls = []
        monkeypatch.setattr(
            trace, "scroll_end", lambda *a, **k: calls.append(1)
        )

        pilot.app._stream_sink.push("answer", "первая строка")
        await pilot.pause(0.7)  # блок смонтирован
        calls.clear()  # изолируем рост от mount-скролла
        pilot.app._stream_sink.push("answer", "\nвторая строка растёт")
        await pilot.pause(0.5)
        assert calls, (
            "ожидался scroll_end на росте блока при стриминге, когда юзер внизу"
        )


@pytest.mark.asyncio
async def test_stream_no_autoscroll_when_scrolled_up(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.containers import VerticalScroll

    tid = "scroll2"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        config={
            "model": "M",
            "backend_api_mode": "chat_completions",
            "tui_stream_display": "answer",
        },
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        pilot.app.active_runner = _FakeRunner()
        monkeypatch.setattr(scr, "_trace_is_at_bottom", lambda: False)
        scr._stick_bottom = False  # юзер отскроллил вверх читать
        trace = pilot.app.query_one("#trace", VerticalScroll)
        calls = []
        monkeypatch.setattr(
            trace, "scroll_end", lambda *a, **k: calls.append(1)
        )

        pilot.app._stream_sink.push("answer", "ответ пока юзер читает выше")
        await pilot.pause(0.7)
        assert not calls, (
            "не должно быть авто-скролла, когда юзер отскроллил вверх"
        )
