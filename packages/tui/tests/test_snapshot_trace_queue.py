"""Очередь у низа ленты (спека P3 §2.4, макет `p3_queue_2`).

Идущий ход: реплика, ответ, `Read` и `Run`, который ещё исполняется
(`· 4 с`); во время хода отправлены две реплики — обе внизу на подложке
`QUEUE_BG`, подсказка только под последней, индикатор хода под очередью.
"""

import json
from types import SimpleNamespace

import pytest
from gc_testkit.tui_support import tui_settings

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")

_TID = "7f3a9b1c"
_QUEUED = ("и добавь тест на ретраи", "потом обнови docs/client.md")


class _BusyRunner:
    """Идущий ход: отправленное уходит в его очередь."""

    def __init__(self) -> None:
        self.added: list[dict] = []

    def is_running(self) -> bool:
        return True

    def is_child_input_active(self) -> bool:
        return False

    def add_message(self, message: dict) -> None:
        self.added.append(message)


def _call(call_id: str, name: str, args: dict) -> dict:
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(args, ensure_ascii=False),
    }


def _turn() -> list[dict]:
    return [
        {
            "id": "u1",
            "role": "user",
            "content": "подними таймаут клиента и проверь тесты",
        },
        {
            "id": "a1",
            "role": "assistant",
            "content": (
                "Посмотрю, где задаётся таймаут и как его читают тесты."
            ),
        },
        _call("c1", "fs_read_file", {"path": "config.py"}),
        {
            "id": "fco_c1",
            "type": "function_call_output",
            "call_id": "c1",
            "output": json.dumps({"tool": "fs_read_file", "ok": True}),
        },
        _call(
            "c2",
            "exec_command",
            {"command": "uv run pytest tests/test_client.py -q"},
        ),
    ]


def _write_task(tmp_path) -> None:
    meta = {
        "thread_id": _TID,
        "status": "running",
        "updated_at": "2026-05-21T12:00:00+00:00",
        "messages_count": 0,
        "tui": {"cwd": "/work/project"},
    }
    (tmp_path / f"meta_cli_{_TID}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{_TID}.jsonl").write_text("", encoding="utf-8")


def _freeze_indicator(scr) -> None:
    """Индикатор хода — 18 с, первая фраза; время кадра не двигает."""
    ind = scr._thinking
    ind._started_at = 0.0
    ind._clock = lambda: 18.0
    rotator = ind._rotator
    rotator._clock = lambda: rotator._last_change
    ind.refresh()


def test_trace_queue_snapshot(snap_compare, tmp_path, monkeypatch):
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen
    from gc_tui.widgets import tool_call_row
    from gc_tui.widgets.command_input import CommandTextArea
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    # Папку в шапке фиксируем — иначе снапшот стал бы машинно-зависимым.
    monkeypatch.setattr(
        TraceScreen, "_current_cwd", lambda self: "/work/demo-project"
    )
    # Живые строки анимированы: дыхание ● и кадр индикатора стоят на
    # первом кадре, время `Run` идёт по своим часам — кадр от нагрузки
    # машины не зависит.
    monkeypatch.setattr(
        tool_call_row.ToolCallRow, "_pulse_tick", lambda self: None
    )
    monkeypatch.setattr(ThinkingIndicator, "_tick", lambda self: None)
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(
        tool_call_row, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    _write_task(tmp_path)

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
        pilot.app.active_runner = _BusyRunner()
        for payload in _turn():
            scr._render_record({"payload": payload}, live=True)
        clock.now += 4
        await pilot.pause()
        for text in _QUEUED:
            await scr.on_command_text_area_submitted(
                CommandTextArea.Submitted(value=text)
            )
            await pilot.pause()
        await pilot.pause(0.3)
        _freeze_indicator(scr)
        scr.query_one("#cmdinput").cursor_blink = False
        await pilot.pause(0.3)

    assert snap_compare(app, terminal_size=(100, 30), run_before=setup)
