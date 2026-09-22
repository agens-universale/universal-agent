"""Multiple submit_solution attempts collapse to one final answer.

Отклонённые intent_guard'ом попытки убираются молча (как regenerate в чатах):
внутренняя кухня качества пользователю не показывается — никаких
"[intent_guard] попытка N отклонена" маркеров в трейсе.
"""
import json

import pytest


@pytest.mark.asyncio
async def test_two_submit_attempts_first_collapsed(tmp_path):
    """If two submit_solution agent events occur, the first should collapse to a marker."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    tid = "t1"
    # Build a data file with two submit_solution events
    lines = []
    lines.append(json.dumps({
        "type": "message", "seq": 1, "ts_utc": "2026-05-21T12:00:00+00:00",
        "payload": {"id": "u1", "role": "user", "content": "анализ"},
    }))
    # First submit_solution
    lines.append(json.dumps({
        "type": "message", "seq": 2, "ts_utc": "2026-05-21T12:00:01+00:00",
        "payload": {
            "id": "fc1", "type": "function_call", "call_id": "c1",
            "name": "submit_solution",
            "arguments": json.dumps({
                "solution_type": "answer_question",
                "task_report": "Первая попытка результата.",
            }),
        },
    }))
    # intent_guard rejection (policy message)
    lines.append(json.dumps({
        "type": "message", "seq": 3, "ts_utc": "2026-05-21T12:00:02+00:00",
        "payload": {
            "role": "user", "id": "p1",
            "content": "<technical type=\"technical\">Finish gate: fix_report.</technical>",
        },
    }))
    # Second submit_solution
    lines.append(json.dumps({
        "type": "message", "seq": 4, "ts_utc": "2026-05-21T12:00:03+00:00",
        "payload": {
            "id": "fc2", "type": "function_call", "call_id": "c2",
            "name": "submit_solution",
            "arguments": json.dumps({
                "solution_type": "answer_question",
                "task_report": "Финальная версия результата.",
            }),
        },
    }))

    (tmp_path / f"data_cli_{tid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "updated_at": "...", "messages_count": 4}),
        encoding="utf-8",
    )

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause()

        # Find all Statics in trace
        statics = pilot.app.query(Static)
        texts = [str(s.render()) for s in statics]
        full_text = "\n".join(texts)

        # First attempt's text should NOT appear in full
        assert "Первая попытка результата" not in full_text
        # Молчаливый коллапс: никакого маркера intent_guard в трейсе
        assert "intent_guard" not in full_text
        assert "отклонена" not in full_text
        # Final attempt visible in full
        assert "Финальная версия результата" in full_text


@pytest.mark.asyncio
async def test_submit_solutions_in_different_turns_not_collapsed(tmp_path):
    """Two submit_solutions separated by a real user message → both kept as full content."""
    pytest.importorskip("textual")
    import json

    from gc_tui.app import TuiApp
    from textual.widgets import Static

    tid = "t1"
    lines = []
    # Turn 1
    lines.append(json.dumps({
        "type": "message", "seq": 1, "ts_utc": "2026-05-22T12:00:00+00:00",
        "payload": {"id": "u1", "role": "user", "content": "первый вопрос"},
    }))
    lines.append(json.dumps({
        "type": "message", "seq": 2, "ts_utc": "2026-05-22T12:00:01+00:00",
        "payload": {
            "id": "fc1", "type": "function_call", "call_id": "c1",
            "name": "submit_solution",
            "arguments": json.dumps({
                "solution_type": "answer_question",
                "task_report": "Ответ на первый вопрос.",
            }),
        },
    }))
    # Turn 2 — real user message
    lines.append(json.dumps({
        "type": "message", "seq": 3, "ts_utc": "2026-05-22T12:00:02+00:00",
        "payload": {"id": "u2", "role": "user", "content": "второй вопрос"},
    }))
    lines.append(json.dumps({
        "type": "message", "seq": 4, "ts_utc": "2026-05-22T12:00:03+00:00",
        "payload": {
            "id": "fc2", "type": "function_call", "call_id": "c2",
            "name": "submit_solution",
            "arguments": json.dumps({
                "solution_type": "answer_question",
                "task_report": "Ответ на второй вопрос.",
            }),
        },
    }))

    (tmp_path / f"data_cli_{tid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "updated_at": "2026-05-22T12:00:00+00:00", "messages_count": 4}),
        encoding="utf-8",
    )

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause()

        statics = pilot.app.query(Static)
        full_text = "\n".join(str(s.render()) for s in statics)

        # BOTH answers should be visible in full
        assert "Ответ на первый вопрос" in full_text
        assert "Ответ на второй вопрос" in full_text
        # NO collapse marker
        assert "intent_guard" not in full_text


@pytest.mark.asyncio
async def test_submit_solution_retry_removes_all_widgets_of_previous_multi_block(tmp_path):
    """Submit_solution с multi-блочным task_report → retry-коллапс удаляет ВСЕ виджеты
    предыдущей попытки, ничего не оставляя взамен (молчаливый regenerate)."""
    pytest.importorskip("textual")
    import json
    from pathlib import Path

    from gc_tui.app import TuiApp

    tid = "collapse_multi"
    # Первая submit_solution с прозой + fenced-блоком (multi-widget рендер).
    first_payload = {
        "type": "function_call",
        "name": "submit_solution",
        "call_id": "c1",
        "arguments": json.dumps({
            "task_report": "Готово:\n```python\nresult = 42\n```\nКонец.",
            "comment": "первая попытка",
        }),
    }
    # Вторая submit_solution (та же call_id не используется — это retry).
    second_payload = {
        "type": "function_call",
        "name": "submit_solution",
        "call_id": "c2",
        "arguments": json.dumps({
            "task_report": "Финальный ответ.",
            "comment": None,
        }),
    }
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "status": "stopped",
                    "updated_at": "2026-05-26T12:00:00+00:00", "messages_count": 2}),
        encoding="utf-8")
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text(
        json.dumps({"type": "message", "seq": 1, "ts_utc": "...", "payload": first_payload}) + "\n"
        + json.dumps({"type": "message", "seq": 2, "ts_utc": "...", "payload": second_payload}) + "\n",
        encoding="utf-8")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)

        scr = pilot.app.screen

        # Code-block-panel первой попытки должен быть удалён.
        code_panels = scr.query(".code-block-panel")
        assert len(code_panels) == 0

        # Continuation-prose первой попытки ("Конец.") тоже удалён.
        # Должны остаться только prose-блоки второй попытки.
        from textual.widgets import Static
        all_text = "\n".join(str(s.render()) for s in scr.query(Static))
        assert "Конец." not in all_text
        assert "отклонена" not in all_text        # маркер юзеру не показываем
        assert "Финальный ответ." in all_text
