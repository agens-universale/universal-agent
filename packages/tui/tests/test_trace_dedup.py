"""S2/A1: два подряд user-facing (kind=agent) ряда с равным контентом → второй
схлопнут в компактную метку (exact-match). Покрывает реальный кейс «проза →
тот же turn_finish/intermediate» — все они мапятся в kind=agent с summary."""

import pytest
from gc_testkit.tui_support import tui_settings


def _prose_rec(text):
    return {
        "payload": {"role": "assistant", "content": text},
        "seq": 1,
        "ts": "2026-06-20T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_duplicate_agent_row_collapsed(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import TraceRow

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._render_records(
            [
                _prose_rec("Готово: всё сделано."),
                _prose_rec("Готово: всё сделано."),
            ]
        )
        await pilot.pause()
        rows = " ".join(
            f"{getattr(r, '_prefix', '')}{getattr(r, '_content', '')}"
            for r in scr.query(TraceRow)
        )
        assert (
            rows.count("Готово: всё сделано.") == 1
        )  # контент один раз (дубль молча опущен)


@pytest.mark.asyncio
async def test_distinct_agent_rows_both_shown(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import TraceRow

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._render_records(
            [_prose_rec("Первая мысль."), _prose_rec("Финальный ответ.")]
        )
        await pilot.pause()
        rows = " ".join(
            f"{getattr(r, '_prefix', '')}{getattr(r, '_content', '')}"
            for r in scr.query(TraceRow)
        )
        assert "Первая мысль." in rows and "Финальный ответ." in rows


def _rec(payload):
    return {"payload": payload, "seq": 1, "ts": "2026-10-19T00:00:00+00:00"}


def _reply(text):
    return _rec({"type": "message", "role": "assistant", "content": text})


def _finish(call_id, report, comment=""):
    import json

    args = {"solution_type": "answer_question", "task_report": report}
    if comment:
        args["comment"] = comment
    return _rec(
        {
            "type": "function_call",
            "name": "turn_finish",
            "call_id": call_id,
            "arguments": json.dumps(args, ensure_ascii=False),
        }
    )


def _rows(scr, cls):
    from gc_tui.widgets.trace_row import TraceRow

    return [
        r._content if isinstance(r._content, str) else r._content.plain
        for r in scr.query(TraceRow)
        if r.has_class(cls)
    ]


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


@pytest.mark.asyncio
async def test_note_survives_report_equal_to_prose(tmp_path):
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._render_records(
            [_reply("Готово."), _finish("f1", "Готово.", "проверь X")]
        )
        await pilot.pause()

        assert _rows(scr, "line-agent") == ["Готово."]
        assert _rows(scr, "line-note") == ["заметка: проверь X"]


@pytest.mark.asyncio
async def test_summary_replaces_second_answer_block(tmp_path):
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._render_records(
            [
                _reply("Пока! 👋"),
                _finish("f1", "Попрощался.", "Без тулов."),
            ]
        )
        await pilot.pause()

        assert _rows(scr, "line-agent") == ["Пока! 👋"]
        assert _rows(scr, "line-summary") == ["итог: Попрощался.\nБез тулов."]
        assert _rows(scr, "line-note") == []


@pytest.mark.asyncio
async def test_rejected_summary_collapsed(tmp_path):
    """Отклонённая попытка с записью «итог» убирается, как блок ответа."""
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._render_records(
            [
                _reply("Пока!"),
                _finish("f1", "Первый отчёт."),
                _rec(
                    {
                        "type": "function_call_output",
                        "call_id": "f1",
                        "output": '{"ok": false}',
                    }
                ),
                _reply("Пока!!"),
                _finish("f2", "Второй отчёт."),
            ]
        )
        await pilot.pause()

        assert _rows(scr, "line-summary") == ["итог: Второй отчёт."]


@pytest.mark.asyncio
async def test_summary_when_poll_splits_response(tmp_path):
    """Текст ответа и вызов finish пришли разными тиками опроса."""
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._render_records([_reply("Пока!")], live=True)
        scr._render_records([_finish("f1", "Попрощался.")], live=True)
        await pilot.pause()

        assert _rows(scr, "line-agent") == ["Пока!"]
        assert _rows(scr, "line-summary") == ["итог: Попрощался."]


@pytest.mark.asyncio
async def test_retry_with_same_report_keeps_report(tmp_path):
    """Повтор отклонённой попытки тем же отчётом: отчёт виден один раз."""
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._render_records(
            [
                _finish("f1", "Отчёт."),
                _rec(
                    {
                        "type": "function_call_output",
                        "call_id": "f1",
                        "output": '{"ok": false}',
                    }
                ),
                _finish("f2", "Отчёт."),
            ]
        )
        await pilot.pause()

        assert _rows(scr, "line-agent") == ["Отчёт."]


@pytest.mark.asyncio
async def test_report_equal_to_previous_turn_prose_is_shown(tmp_path):
    """Дедуп не пересекает границу хода: отчёт этого хода, равный прозе
    прошлого, остаётся ответом."""
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._render_records(
            [
                _reply("Ок."),
                _rec({"role": "user", "content": "ещё раз"}),
                _finish("f1", "Ок."),
            ]
        )
        await pilot.pause()

        assert _rows(scr, "line-agent") == ["Ок.", "Ок."]


def _rejected(call_id):
    return _rec(
        {
            "type": "function_call_output",
            "call_id": call_id,
            "output": '{"ok": false}',
        }
    )


@pytest.mark.asyncio
async def test_prose_equal_to_rejected_report_is_not_repeated(tmp_path):
    """Судья отклонил turn_finish, модель ответила тем же текстом прозой:
    текст виден один раз."""
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._render_records(
            [_finish("f1", "Отчёт."), _rejected("f1"), _reply("Отчёт.")]
        )
        await pilot.pause()

        assert _rows(scr, "line-agent") == ["Отчёт."]


@pytest.mark.asyncio
async def test_retry_after_prose_equal_to_rejected_report_keeps_report(
    tmp_path,
):
    """Отклонённую попытку убирает свёртка: отчёт повтора остаётся, хотя
    проза между попытками была ему равна и не рисовалась."""
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._render_records(
            [
                _finish("f1", "Отчёт."),
                _rejected("f1"),
                _reply("Отчёт."),
                _finish("f2", "Отчёт."),
            ]
        )
        await pilot.pause()

        assert _rows(scr, "line-agent") == ["Отчёт."]


@pytest.mark.asyncio
async def test_retry_report_equal_to_prose_before_rejected_attempt(tmp_path):
    """Проза, отказ судьи текста, отклонённая попытка с другим отчётом,
    повтор отчётом, равным прозе: текст виден один раз."""
    pytest.importorskip("textual")
    from gc_core.message_policy.user_message_policy import (
        wrap_technical_user_content,
    )

    bounce = wrap_technical_user_content("Закрой ход через turn_finish.")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._render_records(
            [
                _reply("Готово."),
                _rec({"role": "user", "content": bounce}),
                _finish("f1", "Сделал другое."),
                _rejected("f1"),
                _finish("f2", "Готово."),
            ]
        )
        await pilot.pause()

        assert _rows(scr, "line-agent") == ["Готово."]
