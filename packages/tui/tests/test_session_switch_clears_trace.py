"""Переключение сессий (attach_active_task) очищает #trace от предыдущей.

Регресс: при переходе одна-сессия → другая трейсы склеивались (виджеты
предыдущего треда оставались в #trace, новый дорисовывался поверх).
"""

from gc_testkit.tui_support import tui_settings
import json

import pytest

_SUMMARY = (
    '<technical type="dialog_summary">Summarized context from previous dialogue.\n'
    "<summary>User asked X.</summary></technical>"
)


def _rec(thread_id, payload):
    return (
        json.dumps(
            {
                "type": "message",
                "thread_id": thread_id,
                "system": "cli",
                "seq": 1,
                "ts": "2026-06-03T10:00:00+00:00",
                "payload": payload,
            },
            ensure_ascii=False,
        )
        + "\n"
    )


def _write(threads_dir, thread_id, payload, with_compaction=False):
    threads_dir.mkdir(exist_ok=True)
    if with_compaction:
        # архив [Seg0] + live [summary, payload] → одна компакция → один маркер
        with open(
            threads_dir
            / f"data_cli_{thread_id}__pre_summary__20260603_095900.jsonl",
            "w",
            encoding="utf-8",
        ) as f:
            f.write(
                _rec(
                    thread_id, {"role": "user", "content": "seg0", "id": "m0"}
                )
            )
        with open(
            threads_dir / f"data_cli_{thread_id}.jsonl", "w", encoding="utf-8"
        ) as f:
            f.write(
                _rec(
                    thread_id,
                    {"role": "user", "content": _SUMMARY, "id": "m_sum"},
                )
            )
            f.write(_rec(thread_id, payload))
    else:
        with open(
            threads_dir / f"data_cli_{thread_id}.jsonl", "w", encoding="utf-8"
        ) as f:
            f.write(_rec(thread_id, payload))
    (threads_dir / f"meta_cli_{thread_id}.json").write_text(
        json.dumps(
            {
                "thread_id": thread_id,
                "status": "stopped",
                "updated_at": "2026-06-03T10:00:00+00:00",
                "messages_count": 1,
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_switch_session_clears_previous_trace(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import SummaryMarker

    threads_dir = tmp_path / "threads"
    tid_a = "sessionAAAA00000000000000000000a"
    tid_b = "sessionBBBB00000000000000000000b"
    _write(
        threads_dir,
        tid_a,
        {"role": "assistant", "content": "ответ A", "id": "m_a"},
        with_compaction=True,
    )
    _write(
        threads_dir,
        tid_b,
        {"role": "assistant", "content": "только ответ B", "id": "m_b"},
    )

    app = TuiApp(
        threads_dir=str(threads_dir),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        screen = app.screen
        # Сессия A — есть summary-маркер.
        screen.attach_active_task(tid_a)
        await pilot.pause()
        assert (
            len(
                [
                    w
                    for w in screen.query(SummaryMarker)
                    if w.has_class("summary-marker")
                ]
            )
            == 1
        )
        # Переключаемся на B — трейс A должен быть очищен.
        screen.attach_active_task(tid_b)
        await pilot.pause()
        assert (
            len(
                [
                    w
                    for w in screen.query(SummaryMarker)
                    if w.has_class("summary-marker")
                ]
            )
            == 0
        ), (
            "summary-маркер сессии A не очищен при переходе на B (склейка трейсов)"
        )
