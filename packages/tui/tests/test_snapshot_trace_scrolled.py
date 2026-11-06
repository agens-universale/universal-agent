"""Лента «не внизу» (спека P3 §2.6, макет `p3_sticky_enter`).

Лента прокручена вверх так, что реплика ушла за верхний край: сверху её
липкая строка с `…`, по центру последней строки ленты — плашка
`↓ в конец · клик или Enter` (поле ввода в фокусе и пустое).
"""

import json

import pytest
from gc_testkit.tui_support import tui_settings

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")

_TID = "7f3a9b1c"
_ASK = (
    "подними таймаут клиента и проверь тесты, потом добавь ретраи "
    "с экспоненциальной задержкой и обнови документацию клиента"
)
# Реплика (две строки) целиком выше верхнего края ленты.
_SCROLL_Y = 17


def _call(call_id: str, name: str, args: dict, output: dict) -> list[dict]:
    return [
        {
            "id": f"fc_{call_id}",
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
        {
            "id": f"fco_{call_id}",
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps({"tool": name, **output}, ensure_ascii=False),
        },
    ]


def _payloads() -> list[dict]:
    answer = "\n".join(
        f"Пункт {i}: ретраи и таймаут клиента." for i in range(1, 25)
    )
    return [
        {"id": "u1", "role": "user", "content": _ASK},
        {
            "id": "a1",
            "role": "assistant",
            "content": (
                "Посмотрю, где задаётся таймаут и как его читают тесты."
            ),
        },
        *_call("c1", "fs_read_file", {"path": "config.py"}, {"ok": True}),
        *_call(
            "c2",
            "exec_command",
            {"command": "uv run pytest tests/test_client.py -q"},
            {"ok": True, "exit_code": 0, "stdout": "3 passed in 0.41s\n"},
        ),
        *_call(
            "c3", "fs_read_file", {"path": "client/retry.py"}, {"ok": True}
        ),
        *_call(
            "c4",
            "fs_find_in_files",
            {"query": "503", "path": "client/"},
            {"ok": True, "count": 1},
        ),
        {"id": "a2", "role": "assistant", "content": answer},
    ]


def _write_thread(tmp_path) -> None:
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
        for seq, payload in enumerate(_payloads(), 1)
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


def test_trace_scrolled_snapshot(snap_compare, tmp_path, monkeypatch):
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen

    # Папку в шапке фиксируем — иначе снапшот стал бы машинно-зависимым.
    monkeypatch.setattr(
        TraceScreen, "_current_cwd", lambda self: "/work/demo-project"
    )
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
        await pilot.pause(0.4)
        inp = scr.query_one("#cmdinput")
        inp.focus()
        inp.cursor_blink = False
        scr.query_one("#trace").scroll_to(y=_SCROLL_Y, animate=False)
        await pilot.pause()
        await pilot.pause(0.3)

    assert snap_compare(app, terminal_size=(100, 30), run_before=setup)
