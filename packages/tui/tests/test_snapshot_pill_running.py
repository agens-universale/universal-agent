"""Плашка «в конец» во время хода (S10 §3.3, §4.2): бар стоит — две
строки, индикатор и нижний ряд; плашка — в нижнем ряду бара, над полем
ввода. Лента прокручена вверх.

Анимация индикатора и его часы заморожены (приём
`test_snapshot_trace_queue`): кадр от нагрузки машины не зависит.
"""

import json

import pytest
from gc_testkit.tui_support import tui_settings

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")

_TID = "7f3a9b1c"
_SCROLL_Y = 4


class _BusyRunner:
    """Идущий ход открытого треда."""

    _thread_kind = "run"
    run_seq = 1

    def __init__(self) -> None:
        self._last_target = (_TID, "cli")

    def is_running(self) -> bool:
        return True

    def is_child_input_active(self) -> bool:
        return False

    def turn_effective_start(self):
        return None


def _write_thread(tmp_path) -> None:
    answer = "\n".join(
        f"Пункт {i}: ретраи и таймаут клиента." for i in range(1, 31)
    )
    payloads = [
        {"id": "u1", "role": "user", "content": "распиши план ретраев"},
        {"id": "a1", "role": "assistant", "content": answer},
    ]
    records = [
        json.dumps(
            {
                "type": "message",
                "seq": seq,
                "ts_utc": "2026-05-21T12:00:00+00:00",
                "payload": payload,
            },
            ensure_ascii=False,
        )
        for seq, payload in enumerate(payloads, 1)
    ]
    (tmp_path / f"data_cli_{_TID}.jsonl").write_text(
        "\n".join(records) + "\n", encoding="utf-8"
    )
    meta = {
        "thread_id": _TID,
        "updated_at": "2026-05-21T12:00:00+00:00",
        "messages_count": len(records),
        "tui": {"cwd": "/work/project"},
    }
    (tmp_path / f"meta_cli_{_TID}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )


def _freeze_indicator(scr) -> None:
    """Индикатор хода — 18 с, первая фраза; время кадра не двигает."""
    ind = scr._thinking
    ind._started_at = 0.0
    ind._clock = lambda: 18.0
    rotator = ind._rotator
    rotator._clock = lambda: rotator._last_change
    ind.refresh()


def test_pill_running_snapshot(snap_compare, tmp_path, monkeypatch):
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    monkeypatch.setattr(
        TraceScreen, "_current_cwd", lambda self: "/work/demo-project"
    )
    monkeypatch.setattr(ThinkingIndicator, "_tick", lambda self: None)
    _write_thread(tmp_path)

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

    async def setup(pilot):
        await pilot.pause()
        scr = pilot.app.screen
        scr.attach_active_task(_TID)
        await pilot.pause(0.3)
        pilot.app.active_runner = _BusyRunner()
        scr._prepare_for_new_run()
        scr._poll_tick()
        await pilot.pause()
        _freeze_indicator(scr)
        inp = scr.query_one("#cmdinput")
        inp.focus()
        inp.cursor_blink = False
        scr.query_one("#trace").scroll_to(y=_SCROLL_Y, animate=False)
        await pilot.pause()
        await pilot.pause(0.3)

    assert snap_compare(app, terminal_size=(100, 30), run_before=setup)
