"""Отрисовщик кусков истории (спека шага 2 окна ленты, §5.2): ходы модели
истории целиком — в ленту, без побочных эффектов на экран; ссылки для
живого состояния — в `ChunkRefs`."""

from __future__ import annotations

import pytest
from feed_dump import (
    HISTORY_THREAD,
    feed_dump,
    load_history_fixture,
    write_history,
)
from textual.app import App
from textual.containers import VerticalScroll
from trace_readonly import model_of, render_model

_SIZE = (120, 60)


class _Feed(App):
    def compose(self):
        yield VerticalScroll(id="feed")


def _feed(app) -> VerticalScroll:
    return app.query_one("#feed", VerticalScroll)


def _model(name: str):
    """Модель фикстуры без сжатий — одной частью треда."""
    from gc_tui.services.history_model import HistoryInputs, build_history

    records = list(load_history_fixture(name).records)
    return build_history(
        [{"messages": records, "compacted_by": None}],
        inputs=HistoryInputs(root=None, injected_fields=()),
    )


def _ev(no, kind, summary="", **kw):
    from gc_tui.models import TraceEvent

    return TraceEvent(
        no=no,
        event_ts_utc_iso="",
        kind=kind,
        title=kind,
        summary=summary,
        full_payload={},
        **kw,
    )


def _texts(feed) -> list[str]:
    return [text for _cls, text, _classes in feed_dump(feed)]


async def _render(model, **options):
    """Все ходы модели в пустую ленту: (дамп, ссылки куска)."""
    async with _Feed().run_test(size=_SIZE) as pilot:
        refs = render_model(_feed(pilot.app), model, **options)
        await pilot.pause()
        return feed_dump(_feed(pilot.app)), refs


@pytest.mark.asyncio
async def test_attempts_collapsed_per_turn():
    """Отклонённая попытка turn_finish уходит, когда пришла следующая;
    итог прошлого хода следующий ход не трогает."""
    rows, _refs = await _render(_model("finish_attempts"))
    reports = [text for _c, text, _cl in rows if "Итог хода" in text]
    assert len(reports) == 2
    assert "Итог хода 1" in reports[0]
    assert "Итог хода 2" in reports[1]


@pytest.mark.asyncio
async def test_dedup_resets_on_any_user_event():
    """Ответ, повторивший прошлый, не рисуется — до любого события
    пользователя, включая ответ на вопрос агента."""
    model = model_of(
        _ev(1, "user", "вопрос"),
        _ev(2, "agent", "Готово."),
        _ev(3, "agent", "Готово."),
        _ev(4, "user", "да", tool_name="ask_human", call_id="q1"),
        _ev(5, "agent", "Готово."),
    )
    rows, _refs = await _render(model)
    assert [t for t in (r[1] for r in rows) if "Готово." in t] == [
        "●\nГотово.",
        "●\nГотово.",
    ]


@pytest.mark.asyncio
async def test_plan_panel_rendered():
    from gc_tui.widgets.plan_panel import PlanPanel

    model = _model("plan")
    async with _Feed().run_test(size=_SIZE) as pilot:
        render_model(_feed(pilot.app), model)
        await pilot.pause()
        (grey,) = _feed(pilot.app).query(PlanPanel)
        assert not grey.has_class("plan-accepted")
    async with _Feed().run_test(size=_SIZE) as pilot:
        seen = []
        render_model(
            _feed(pilot.app),
            model,
            plan_accepted=lambda text: seen.append(text) or True,
        )
        await pilot.pause()
        (panel,) = _feed(pilot.app).query(PlanPanel)
        assert panel.has_class("plan-accepted")
    assert len(seen) == 1
    assert seen[0].startswith("## План, ход 1")


@pytest.mark.asyncio
async def test_withdrawn_not_rendered():
    """Реплика, снятая откатом, остаётся в модели, но не рисуется, и
    строки итога её хода нет."""
    from gc_tui.widgets.turn_line import TurnLine

    model = _model("finish_attempts")
    reply = model.items[model.turn_starts[1]].event
    model.mark_withdrawn(reply.message_id)
    async with _Feed().run_test(size=_SIZE) as pilot:
        render_model(_feed(pilot.app), model)
        await pilot.pause()
        texts = _texts(_feed(pilot.app))
        openers = [w.opened_by for w in _feed(pilot.app).query(TurnLine)]
    assert not any("turn 2" in t for t in texts)
    assert any("turn 1" in t for t in texts)
    assert reply.message_id not in openers
    assert len(openers) == 1


@pytest.mark.asyncio
async def test_question_rows_answered_and_unanswered_without_banner():
    """Вопрос агента в истории — видимая строка, ответ или «без ответа»
    под ней (по модели), без баннера; строки вопросов — в ссылках."""
    from gc_tui.widgets.ask_human_banner import AskHumanBanner

    answered = _model("ask_human")
    rows, refs = await _render(answered)
    assert len(refs.question_rows) == 1
    assert all("hidden" not in classes for _c, _t, classes in rows)
    assert not any("без ответа" in text for _c, text, _cl in rows)

    model = model_of(
        _ev(1, "user", "сделай"),
        _ev(2, "ask_human", "А или Б?", call_id="q1"),
        _ev(3, "system", "[ask_human] пусто", tool_name="ask_human",
            call_id="q1"),
    )  # fmt: skip
    async with _Feed().run_test(size=_SIZE) as pilot:
        refs = render_model(_feed(pilot.app), model)
        await pilot.pause()
        rows = feed_dump(_feed(pilot.app))
        assert not list(pilot.app.query(AskHumanBanner))
    assert list(refs.question_rows) == ["q1"]
    unanswered = [text for _c, text, _cl in rows if "без ответа" in text]
    assert len(unanswered) == 1
    assert "А или Б?" in unanswered[0]
    assert len(rows) == 2


def _choice(choice: str, alias: str, before: str) -> dict:
    return {
        "type": "model_choice",
        "ts": "",
        "model": {
            "v": 1,
            "choice": choice,
            "alias": alias,
            "before": before,
        },
    }


def _marks_model():
    """Две смены модели на стыке ходов: реплика между ними снята
    откатом."""
    from gc_tui.services.history_model import HistoryModel

    model = HistoryModel()
    for no, rec in (
        (1, None),
        (None, _choice("max", "strong-model", "fast-model")),
        (2, None),
        (None, _choice("light", "light-model", "strong-model")),
        (3, None),
    ):
        if rec is not None:
            model.add_model_choice(rec)
            continue
        model.turn_starts.append(len(model.items))
        model.append_event(
            _ev(no, "user", f"реплика {no}", message_id=f"m{no}")
        )
    model.mark_withdrawn("m2")
    return model


@pytest.mark.asyncio
async def test_model_marks_collapse_across_chunk_boundary():
    """Черта на стыке кусков схлопывается с соседней так же, как подряд —
    и когда кусок ниже уже стоит, а верхний вставлен перед ним."""
    from gc_tui.services.history_render import (
        ChunkOptions,
        FeedHost,
    )
    from gc_tui.widgets.model_mark import ModelMark
    from trace_readonly import render_turns

    model = _marks_model()
    whole, _refs = await _render(model)
    assert [c for c, _t, _cl in whole].count("ModelMark") == 1
    async with _Feed().run_test(size=_SIZE) as pilot:
        host = FeedHost(_feed(pilot.app))
        opts = ChunkOptions()
        lower = render_turns(
            host, model, range(1, 3), before=None, options=opts
        )
        render_turns(
            host, model, range(1), before=lower.widgets[0], options=opts
        )
        await pilot.pause()
        assert feed_dump(_feed(pilot.app)) == whole
        (mark,) = _feed(pilot.app).query(ModelMark)
        assert mark.before == "fast-model"


@pytest.mark.asyncio
@pytest.mark.parametrize("focusable", [True, False])
async def test_compaction_refs_returned_and_focusable_by_option(
    tmp_path, focusable
):
    from gc_tui.services.history_model import HistoryInputs, build_history
    from gc_tui.services.stores.file_store import FileTaskStore

    write_history(str(tmp_path), load_history_fixture("compaction"))
    segments, _cursor = FileTaskStore(
        threads_dir=str(tmp_path), system="cli"
    ).read_history(HISTORY_THREAD)
    model = build_history(
        segments, inputs=HistoryInputs(root=None, injected_fields=())
    )
    markers = [it.event.no for it in model.items if it.kind == "compaction"]
    _rows, refs = await _render(model, focusable_compaction=focusable)
    assert markers
    assert list(refs.summary_markers) == markers
    assert list(refs.summary_bodies) == markers
    assert all(m.can_focus is focusable for m in refs.summary_markers.values())


@pytest.mark.asyncio
async def test_only_last_turn_line_fresh():
    from gc_tui.widgets.turn_line import TurnLine

    model = _model("model_change")
    async with _Feed().run_test(size=_SIZE) as pilot:
        refs = render_model(_feed(pilot.app), model)
        await pilot.pause()
        lines = list(_feed(pilot.app).query(TurnLine))
        assert [line.is_fresh for line in lines] == [False, False, True]
        assert refs.fresh_turn_line is lines[-1]

    model.supersede_outcome()
    async with _Feed().run_test(size=_SIZE) as pilot:
        refs = render_model(_feed(pilot.app), model)
        await pilot.pause()
        lines = list(_feed(pilot.app).query(TurnLine))
        assert not any(line.is_fresh for line in lines)
        assert refs.fresh_turn_line is None


@pytest.mark.asyncio
async def test_withdrawn_last_turn_falls_back_to_previous_line():
    """Откат реплики последнего хода, чей итог уже в модели: итог и
    свежая строка — прошлого хода (как `_close_undone_run` вживую)."""
    from gc_core.persistence.turn_outcome import parse_turn_outcome
    from gc_tui.widgets.turn_line import TurnLine

    model = _model("model_change")
    openers = [
        parse_turn_outcome(it.side).opened_by
        for it in model.items
        if it.kind == "outcome"
    ]
    model.mark_withdrawn(openers[-1])
    model.restore_outcome()
    assert model.last_turn_outcome.opened_by == openers[-2]
    async with _Feed().run_test(size=_SIZE) as pilot:
        refs = render_model(_feed(pilot.app), model)
        await pilot.pause()
        lines = list(_feed(pilot.app).query(TurnLine))
        assert [line.opened_by for line in lines] == openers[:-1]
        assert refs.fresh_turn_line is lines[-1]
        assert lines[-1].is_fresh


@pytest.mark.asyncio
async def test_levels_namespaced():
    from gc_tui.services.history_render import HISTORY_LEVEL_PREFIX

    _rows, refs = await _render(_model("calls"))
    assert len(refs.block_views) > 1
    assert all(
        level.startswith(HISTORY_LEVEL_PREFIX) for level in refs.block_views
    )
    assert all(
        row.block_key[0].startswith(HISTORY_LEVEL_PREFIX)
        for row in refs.call_rows.values()
    )


@pytest.mark.asyncio
async def test_render_turns_does_not_touch_live_state(tmp_path):
    """Кусок в ленте экрана сессии не трогает живого состояния экрана:
    стрим, вызовы без исхода, баннер вопроса, счётчик «в конец», модель
    истории, открытый тред."""
    from gc_testkit.tui_support import tui_settings
    from gc_tui.app import TuiApp
    from gc_tui.services.history_render import (
        ChunkOptions,
        FeedHost,
    )
    from trace_readonly import render_turns

    model = model_of(
        _ev(1, "user", "сделай"),
        _ev(2, "call", call_id="c1", tool_name="fs_read_file"),
        _ev(3, "ask_human", "А или Б?", call_id="q1"),
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        await pilot.pause()
        history = scr._feed_history
        before = (
            scr._stream_block,
            scr._stream_answer_row,
            dict(scr._active_calls),
            scr._ask_human_banner,
            scr._ask_human_call_id,
            scr._new_replies,
            len(history.items),
            scr._active_task_id,
            dict(scr._question_rows),
            pilot.app.tui_state.ask_human_state,
        )
        refs = render_turns(
            FeedHost(scr.query_one("#trace")),
            model,
            range(model.turn_count()),
            before=None,
            options=ChunkOptions(),
        )
        await pilot.pause()
        after = (
            scr._stream_block,
            scr._stream_answer_row,
            dict(scr._active_calls),
            scr._ask_human_banner,
            scr._ask_human_call_id,
            scr._new_replies,
            len(scr._feed_history.items),
            scr._active_task_id,
            dict(scr._question_rows),
            pilot.app.tui_state.ask_human_state,
        )
        assert scr._feed_history is history
        # Виды блоков куска — не в живом реестре (верхний заводит тик).
        assert not set(scr._block_views) & set(refs.block_views)
    assert after == before
    assert list(refs.call_rows) == ["c1"]
    assert list(refs.question_rows) == ["q1"]


_ANSWER = (
    "## Итог\n\nТекст **жирный**.\n\n"
    "```python\nprint('x')\n```\n\nЕщё абзац.\n\n"
    "| a | b |\n|---|---|\n| 1 | 2 |\n\nКонец."
)


def _prose_turns(n: int):
    """Ходы без вызовов: реплика и ответ в markdown — много виджетов."""
    from gc_tui.services.history_model import HistoryInputs, build_history

    records = []
    for i in range(n):
        records += [
            {
                "type": "message",
                "payload": {
                    "role": "user",
                    "id": f"m_u{i}",
                    "content": f"запрос {i}",
                },
            },
            {
                "type": "message",
                "payload": {
                    "id": f"msg_a{i}",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "annotations": [],
                            "text": _ANSWER,
                        }
                    ],
                },
            },
        ]
    return build_history(
        [{"messages": records, "compacted_by": None}],
        inputs=HistoryInputs(root=None, injected_fields=()),
    )


@pytest.mark.asyncio
async def test_chunk_mounts_feed_in_one_call():
    """Кусок без блоков вызовов уходит в ленту одним `mount`: каждый
    вызов `Widget.mount` кладёт в очередь ленты снимок всех её детей, и
    поштучный монтаж при подъёме копит их быстрее, чем лента разбирает."""
    from gc_tui.services.history_render import (
        ChunkOptions,
        FeedHost,
    )
    from trace_readonly import render_turns

    batches: list[int] = []

    class _Counting(FeedHost):
        def mount(self, *widgets, before=None, after=None) -> None:
            batches.append(len(widgets))
            super().mount(*widgets, before=before, after=after)

    model = _prose_turns(3)
    async with _Feed().run_test(size=_SIZE) as pilot:
        feed = _feed(pilot.app)
        render_turns(
            _Counting(feed),
            model,
            range(model.turn_count()),
            before=None,
            options=ChunkOptions(),
        )
        await pilot.pause()
        assert batches == [len(feed.children)]


def test_result_kinds_are_the_renderer_result_handlers():
    """Страж одного источника: события, чьи виджеты встают якорем к
    строке вызова у отрисовщика, — те же исходы, что модель и сплиттер
    шагов считают исходами (`trace_steps.RESULT_KINDS`)."""
    from gc_tui.services.history_render import _HANDLERS, _Chunk
    from gc_tui.services.trace_steps import RESULT_KINDS

    anchored = {
        kind
        for kind, handler in _HANDLERS.items()
        if handler in (_Chunk._result, _Chunk._error_msg)
    }
    assert anchored == RESULT_KINDS
