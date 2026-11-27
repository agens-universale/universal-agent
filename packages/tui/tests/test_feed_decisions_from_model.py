"""Решения ленты — по модели истории и реестрам, а не по смонтированным
виджетам (спека шага 2 окна ленты, §5.5).

Окно снимает старые ходы, но держит смонтированными ход в фокусе и строки
со ссылками живого состояния: смонтированным может остаться более старый
ход, когда новые сняты. Снятие здесь — ``remove()`` виджета, из которого
решение бралось раньше; решение должно остаться тем же, что при полной
ленте.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from feed_dump import HISTORY_THREAD, load_history_fixture, write_history
from gc_testkit.tui_support import tui_settings
from gc_tui.models import TraceEvent

_SETTINGS = {
    "agent.model": "M",
    "models": {
        "fast-model": {"url": "http://127.0.0.1:9/v1", "token": ""},
        "strong-model": {"url": "http://127.0.0.1:9/v1", "token": ""},
    },
    "slots": {"default": "fast-model", "max": "strong-model"},
}
_SIZE = (160, 45)


def _app(threads_dir: Path):
    # Импорт приложения — в тесте: палитра выбирается до импорта gc_tui.app.
    from gc_tui.app import TuiApp

    threads_dir.mkdir(exist_ok=True)
    return TuiApp(
        threads_dir=str(threads_dir),
        system="cli",
        settings=tui_settings(_SETTINGS),
    )


async def _opened(pilot, threads_dir: Path, fixture: str):
    """Экран сессии, открытой на треде фикстуры."""
    write_history(str(threads_dir), load_history_fixture(fixture))
    scr = pilot.app.screen
    assert scr.attach_active_task(HISTORY_THREAD, is_resume=True)
    await pilot.pause()
    return scr


async def _fresh(pilot, threads_dir: Path):
    """Экран сессии на пустом треде."""
    write_history(str(threads_dir), load_history_fixture("calls"))
    scr = pilot.app.screen
    assert scr.attach_active_task(HISTORY_THREAD, is_resume=True)
    await pilot.pause()
    scr._clear_trace()
    scr._reset_render_state()
    await pilot.pause()
    return scr


def _event(no: int, kind: str, summary: str = "", **fields) -> TraceEvent:
    return TraceEvent(
        no=no,
        event_ts_utc_iso="",
        kind=kind,
        title=kind,
        summary=summary,
        full_payload={},
        **fields,
    )


def _draw(scr, ev: TraceEvent) -> None:
    """Событие — в модель и в ленту, как живой путь."""
    scr._feed_history.append_event(ev)
    scr._draw_event(ev, live=False)


@pytest.mark.asyncio
async def test_model_mark_follows_model_when_last_mark_unmounted(tmp_path):
    """Последняя черта смены модели снята (её ход ушёл из окна), черта
    старого хода осталась — лента всё равно показывает последнюю смену."""
    from gc_tui.widgets.model_mark import ModelMark

    threads_dir = tmp_path / "threads"
    async with _app(threads_dir).run_test(size=_SIZE) as pilot:
        scr = await _opened(pilot, threads_dir, "model_change")
        marks = list(scr.query(ModelMark))
        assert [m.choice for m in marks] == ["max", "default"]
        full = scr._last_model_choice()
        marks[-1].remove()
        await pilot.pause()
        assert scr._last_model_choice() == full == "default"


@pytest.mark.asyncio
async def test_unanswered_note_when_question_row_unmounted(tmp_path):
    """Строка вопроса снята — «без ответа» не рисуется отдельной строкой:
    пометка принадлежит вопросу."""
    from gc_tui.widgets.trace_row import QuestionRow, TraceRow

    threads_dir = tmp_path / "threads"
    async with _app(threads_dir).run_test(size=_SIZE) as pilot:
        scr = await _fresh(pilot, threads_dir)
        _draw(
            scr,
            _event(1, "ask_human", "куда?", call_id="q1", tool_name="ask"),
        )
        await scr.query_one(QuestionRow).remove()
        _draw(
            scr,
            _event(
                2,
                "system",
                "[ask_human] без ответа",
                call_id="q1",
                tool_name="ask_human",
            ),
        )
        await pilot.pause()
        trace = scr.query_one("#trace")
        assert [w for w in trace.children if isinstance(w, TraceRow)] == []


@pytest.mark.asyncio
async def test_trace_note_owner_when_image_reply_unmounted(tmp_path):
    """Реплика с картинками снята, более поздняя без картинок на месте —
    пометка не уходит чужой реплике: её владелец не на экране."""
    from gc_core.vision.notes import TraceNote
    from image_helpers import make_attachment

    threads_dir = tmp_path / "threads"
    async with _app(threads_dir).run_test(size=_SIZE) as pilot:
        scr = await _fresh(pilot, threads_dir)
        _draw(
            scr,
            _event(
                1,
                "user",
                "вот",
                message_id="u1",
                attachments=(make_attachment("f" * 64, 1),),
            ),
        )
        _draw(scr, _event(2, "user", "и ещё", message_id="u2"))
        await pilot.pause()
        note = TraceNote("не просмотрено", "reply")
        with_images = scr._trace_note_owner(note)
        assert with_images.has_images
        with_images.remove()
        await pilot.pause()
        assert scr._trace_note_owner(note) is None


@pytest.mark.asyncio
async def test_plan_panel_state_applied_on_mount(tmp_path):
    """Рамка плана получает состояние при вставке в ленту — и от сессии,
    и от решения человека, ещё не дошедшего до сессии."""
    from gc_core.plan_store import PLAN_STATUS_APPROVED
    from gc_tui.services.trace_mapper import PROPOSE_PLAN_ID
    from gc_tui.widgets.plan_panel import PlanPanel

    threads_dir = tmp_path / "threads"
    async with _app(threads_dir).run_test(size=_SIZE) as pilot:
        scr = await _fresh(pilot, threads_dir)
        plan = {"text": "сделать", "status": PLAN_STATUS_APPROVED}
        scr._session_plan = lambda: plan
        _draw(scr, _event(1, "agent", "сделать", tool_name=PROPOSE_PLAN_ID))
        panel = scr.query_one(PlanPanel)
        assert panel.has_class("plan-accepted")
        # Человек вернул план в обсуждение, рамка снята; рамка того же плана
        # вставлена снова (ход домонтирован).
        scr._mark_plan_accepted(False, "сделать")
        await panel.remove()
        _draw(scr, _event(2, "user", "дальше", message_id="u2"))
        _draw(scr, _event(3, "agent", "сделать", tool_name=PROPOSE_PLAN_ID))
        assert not scr.query_one(PlanPanel).has_class("plan-accepted")


@pytest.mark.asyncio
async def test_command_output_detected_after_trim(tmp_path):
    """Команда дописала строку, а снятие старого хода убрало столько же —
    вывод команды всё равно возвращает ленту вниз."""
    from gc_tui.widgets.trace_row import TraceRow

    threads_dir = tmp_path / "threads"
    async with _app(threads_dir).run_test(size=_SIZE) as pilot:
        scr = await _opened(pilot, threads_dir, "calls")

        trace = scr.query_one("#trace")

        async def dispatch(_raw: str) -> None:
            old = next(w for w in trace.children if isinstance(w, TraceRow))
            await old.remove()
            scr._mount_trace_row(
                prefix="· ", content="вывод", kind_class="line-meta"
            )

        scr._dispatch_command = dispatch
        scr._stick_bottom = False
        await scr._handle_command("/что-то")
        assert scr._stick_bottom is True


@pytest.mark.asyncio
async def test_undo_freshens_previous_turn_line_when_unmounted(tmp_path):
    """Откат хода: строка прошлого хода снята, строка более старого хода
    осталась (ход в фокусе) — свежей становится не она; прошлый ход по
    модели снова последний."""
    from gc_tui.widgets.turn_line import TurnLine

    threads_dir = tmp_path / "threads"
    async with _app(threads_dir).run_test(size=_SIZE) as pilot:
        scr = await _opened(pilot, threads_dir, "model_change")
        lines = list(scr.query(TurnLine))
        assert [line.is_fresh for line in lines] == [False, False, True]
        previous = lines[-1].opened_by
        # Начался новый ход — строка прошлого осела.
        scr._feed_history.supersede_outcome()
        scr._settle_turn_line()
        lines[-1].remove()
        await pilot.pause()
        scr._close_undone_run(None, "m_undone")
        assert [line.is_fresh for line in lines[:-1]] == [False, False]
        assert scr._latest_turn_outcome().opened_by == previous
