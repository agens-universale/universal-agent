"""Память эфемерного и снятый ход (спека шага 2 окна ленты, §3.1, §3.5,
§5.4, §8 «Снять и вернуть», «Эфемерное»).

Ход, нарисованный живым путём, снимается с ленты (`forget_turn`) и
возвращается отрисовщиком кусков (`render_turns`) таким, каким был снят:
мысли, «!», вывод команд, пометки, вложенный трейс, раскрытое — на
месте. После смены сессии, пересборки и ухода хода за окно удержания —
как после переоткрытия."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from feed_dump import (
    HISTORY_THREAD,
    feed_dump,
    feed_live,
    load_history_fixture,
    without_ephemeral,
)
from gc_core.vision.notes import TraceNote
from gc_testkit.tui_support import pinned_snapshot_clock, tui_settings
from gc_tui.services.trace_ephemera import Ephemera
from gc_tui.services.trace_note_sink import TraceNoteSink
from test_feed_parity_baseline import _settled_dump

_SIZE = (160, 45)
_SETTINGS = {"agent.model": "M", "tui.show_reasoning": "on"}
_LONG_ERROR = "сбой провайдера: " + "подробности причины " * 20


def _app(threads_dir):
    from gc_tui.app import TuiApp

    threads_dir.mkdir()
    return TuiApp(
        threads_dir=str(threads_dir),
        system="cli",
        settings=tui_settings(_SETTINGS),
    )


# ---- записи двух ходов ------------------------------------------------------


def _records() -> list[dict]:
    """Фикстура субагентов (два хода), а в первый ход — команда с длинным
    выводом, вопрос агента без ответа и группа из четырёх чтений; итог
    первого хода — сбой с длинной причиной."""
    sub = list(load_history_fixture("subagents").records)
    calls = load_history_fixture("calls").records
    ask = load_history_fixture("ask_human").records
    unanswered = json.loads(json.dumps(ask[5]))
    output = json.loads(unanswered["payload"]["output"])
    output.update(ok=False, response="(empty response)")
    unanswered["payload"]["output"] = json.dumps(output)
    failed = json.loads(json.dumps(sub[8]))
    failed["outcome"].update(status="error", error={"message": _LONG_ERROR})
    explored = [calls[i] for i in (6, 7, 16, 17, 18, 19, 21, 22, 29, 30)]
    first = [*sub[:4], calls[2], calls[3], ask[4], unanswered, *explored]
    return [*first, *sub[4:8], failed, *sub[9:]]


_CALLS = load_history_fixture("calls").records
_EXEC_CALL = _CALLS[2]["payload"]["call_id"]
_GROUP_CALL = _CALLS[6]["payload"]["call_id"]
_AGENT_CALL = load_history_fixture("subagents").records[2]["payload"][
    "call_id"
]
#: Записи первого хода — до его итога включительно.
_FIRST_TURN_END = 23
_PLAN = "## План\n- шаг первый\n- шаг второй"
_FIRST_REPLY = load_history_fixture("subagents").records[0]["payload"]["id"]


def _sidecar() -> list[dict]:
    """Трасса субагента фикстуры и ещё две команды: окно субагента
    скрывает часть событий (раскрываемо)."""
    sidecar = list(load_history_fixture("subagents").sidecar)
    extra = []
    for n in (1, 2):
        for rec in sidecar[2:4]:
            copy = json.loads(json.dumps(rec))
            copy["payload"]["call_id"] += f"x{n}"
            if "id" in copy["payload"]:
                copy["payload"]["id"] += f"x{n}"
            extra.append(copy)
    return [*sidecar[:4], *extra, *sidecar[4:]]


def _thought(attempt: str, text: str):
    from gc_tui.services.stream_sink import ThoughtSnapshot

    return ThoughtSnapshot(attempt, text, 2.0, False, datetime.now(UTC))


async def live_session(pilot, prelude: list[dict] | None = None):
    """Экран на пустом треде; первый ход — живым путём с эфемерным: мысль,
    блок «!», строка команды, пометка трейса под вызовом, после итога —
    копия рамки плана над свежей строкой и реплика, ставшая «не
    отправлено»; второй ход — текущий. ``prelude`` — записи ходов до
    них."""
    scr = pilot.app.screen
    assert scr.attach_active_task(HISTORY_THREAD)
    if prelude:
        feed_live(scr, prelude, splits=())
    records = _records()
    sidecar = _sidecar()
    feed_live(scr, records[:1], splits=())
    scr._sync_thoughts([_thought("a1", "думаю над ходом\n" * 8)])
    scr._settle_thoughts()
    scr._mount_bang_row(
        prefix="[!] ", content="$ echo hi", kind_class="line-bang"
    )
    scr._mount_bang_row(prefix="   ", content="hi", kind_class="line-bang")
    scr._close_bang_block()
    scr._mount_trace_row(
        prefix="", content="/model: выбор не изменён", kind_class="line-meta"
    )
    feed_live(scr, records[1:8], splits=(), sidecar=sidecar)
    pilot.app._trace_note_sink = TraceNoteSink()
    pilot.app._trace_note_sink.push(
        HISTORY_THREAD,
        TraceNote("картинка не принята", owner="call", call_id=_EXEC_CALL),
    )
    scr._drain_trace_notes()
    feed_live(scr, records[8:_FIRST_TURN_END], splits=(), sidecar=sidecar)
    scr._ensure_plan_panel_last({"text": _PLAN}, above_fresh_line=True)
    scr._mount_user_reply("черновик", state="pending", client_id="cx")
    scr._mark_pending_failed(None)
    feed_live(scr, records[_FIRST_TURN_END:], splits=(), sidecar=sidecar)
    await pilot.pause()
    return scr


def _expand_views(scr) -> None:
    """Раскрыть: вывод команды, группу чтений, окно субагента, причину
    итога первого хода, мысль."""
    from gc_tui.services.tool_blocks import (
        AGENT_WINDOW,
        OUTPUT_SLOT,
        phrase_key,
    )
    from gc_tui.widgets.turn_line import TurnLine

    view = scr._block_views[""]
    block = view.model.block_of(_EXEC_CALL)
    view.toggle(phrase_key(block, OUTPUT_SLOT))
    view.toggle(view.model.block_of(_GROUP_CALL))
    scr._block_views[_AGENT_CALL].toggle(AGENT_WINDOW)
    (line,) = [w for w in scr.query(TurnLine) if w.opened_by == _FIRST_REPLY]
    line.toggle()
    scr._streamed_thoughts[0].toggle()


def restore(scr, turn: int):
    """Снятый верхний ход — обратно отрисовщиком кусков (как кусок
    сверху окна)."""
    refs = scr._window_render(range(turn, turn + 1), before=scr._window_top())
    scr._window_first = turn
    scr._window_adopt_chunk(refs)
    return refs


def _trace(scr):
    return scr.query_one("#trace")


# ---- снять и вернуть ---------------------------------------------------------


@pytest.mark.asyncio
async def test_trim_restore_roundtrip(tmp_path, monkeypatch):
    with pinned_snapshot_clock(monkeypatch):
        async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
            scr = await live_session(pilot)
            _expand_views(scr)
            before = await _settled_dump(pilot)
            ephemeral = [
                r for r in before if r not in without_ephemeral(before)
            ]
            assert {cls for cls, _t, _c in ephemeral} >= {
                "ThoughtRow",
                "TraceRow",
            }
            assert any("картинка не принята" in t for _c, t, _ in before)
            assert any("без ответа" in t for _c, t, _ in before)
            assert any("-in-agent" in c for _c, _t, c in before)
            assert any("-expanded" in c for _c, _t, c in before)
            texts = [t for _c, t, _ in before]
            # Свёрнуть: группу чтений и окно субагента.
            assert texts.count("    − свернуть группу") == 1
            assert sum("свернуть группу" in t for t in texts) == 2
            assert any("черновик" in t for t in texts)
            line = next(i for i, t in enumerate(texts) if "Ошибка" in t)
            assert "предложенный план" not in texts[line + 1]

            assert scr.forget_turn(0)
            await pilot.pause()
            assert not any(
                "echo hi" in t for _c, t, _ in feed_dump(_trace(scr))
            )
            restore(scr, 0)
            assert await _settled_dump(pilot) == before


@pytest.mark.asyncio
async def test_trim_twice_restores_again(tmp_path, monkeypatch):
    """Вернувшийся ход снова снимается и снова возвращается тем же."""
    with pinned_snapshot_clock(monkeypatch):
        async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
            scr = await live_session(pilot)
            _expand_views(scr)
            before = await _settled_dump(pilot)
            for _ in range(2):
                assert scr.forget_turn(0)
                await pilot.pause()
                restore(scr, 0)
                assert await _settled_dump(pilot) == before


@pytest.mark.asyncio
async def test_ephemera_before_first_reply_restored(tmp_path, monkeypatch):
    """Блок «!» в пустой сессии до первой реплики встаёт раньше любого
    хода и в окно ни одного хода не входит: снятие первого хода его не
    уносит, ход возвращается под него — порядок ленты прежний."""
    records = _records()
    with pinned_snapshot_clock(monkeypatch):
        async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
            scr = pilot.app.screen
            assert scr.attach_active_task(HISTORY_THREAD)
            scr._mount_bang_row(
                prefix="[!] ", content="$ echo early", kind_class="line-bang"
            )
            scr._close_bang_block()
            feed_live(scr, records, splits=(), sidecar=_sidecar())
            before = await _settled_dump(pilot)
            assert "echo early" in before[0][1]
            assert scr.forget_turn(0)
            await pilot.pause()
            after = feed_dump(_trace(scr))
            assert "echo early" in after[0][1]
            assert "turn 1" not in after[1][1]
            restore(scr, 0)
            assert await _settled_dump(pilot) == before


@pytest.mark.asyncio
async def test_thoughts_follow_current_flag(tmp_path):
    """Снятая мысль возвращается по нынешнему флагу показа мыслей и
    дальше следует его сменам."""
    from gc_tui.widgets.thought_row import ThoughtRow

    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await live_session(pilot)
        scr._sync_thoughts_flag()
        assert scr.forget_turn(0)
        monkeypatch_flag(scr, False)
        await pilot.pause()
        restore(scr, 0)
        (row,) = _trace(scr).query(ThoughtRow)
        assert not row.display
        monkeypatch_flag(scr, True)
        assert row.display


def monkeypatch_flag(scr, shown: bool) -> None:
    """Сменить действующий флаг мыслей так, как его сверяет экран."""
    scr._thoughts_on = lambda: shown
    scr._sync_thoughts_flag()


@pytest.mark.asyncio
async def test_withdrawn_not_resurrected_after_trim(tmp_path):
    """Реплика, снятая откатом, и строки, которые экран снял сам, после
    снятия хода и возврата не воскресают."""
    from gc_tui.widgets.user_reply import UserReplyContainer

    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await live_session(pilot)
        (reply,) = [
            w for w in _trace(scr).query(UserReplyContainer) if w.event_no == 1
        ]
        scr._withdraw_from_history(_FIRST_REPLY)
        reply.remove()
        row = scr._mount_trace_row(
            prefix="", content="строка экрана", kind_class="line-meta"
        )
        await pilot.pause()
        row.remove()
        await pilot.pause()
        assert scr.forget_turn(0)
        await pilot.pause()
        restore(scr, 0)
        await pilot.pause()
        texts = [t for _c, t, _ in feed_dump(_trace(scr))]
        assert not any("turn 1 calls=2" in t for t in texts)
        assert not any("строка экрана" in t for t in texts)
        assert any("echo hi" in t for t in texts)


@pytest.mark.asyncio
async def test_bang_clear_after_trim(tmp_path):
    """`! clear N` считает и блоки снятых ходов: `! clear 1` уносит живой
    последний блок, снятый остаётся; следующий `! clear 1` уносит снятый —
    он не возвращается."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await live_session(pilot)
        scr._mount_bang_row(
            prefix="[!] ", content="$ late", kind_class="line-bang"
        )
        scr._close_bang_block()
        assert scr.forget_turn(0)
        await pilot.pause()
        scr._clear_bang_blocks(1)
        await pilot.pause()
        restore(scr, 0)
        await pilot.pause()
        texts = [t for _c, t, _ in feed_dump(_trace(scr))]
        assert any("echo hi" in t for t in texts)
        assert not any("$ late" in t for t in texts)
        assert scr.forget_turn(0)
        await pilot.pause()
        scr._clear_bang_blocks(1)
        await pilot.pause()
        restore(scr, 0)
        await pilot.pause()
        texts = [t for _c, t, _ in feed_dump(_trace(scr))]
        assert not any("echo hi" in t for t in texts)
        assert scr._ephemera.bang_clear(None) == []


async def _forgotten_and_back(pilot, scr) -> list:
    assert scr.forget_turn(0)
    await pilot.pause()
    restore(scr, 0)
    return await _settled_dump(pilot)


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["rebuild", "switch"])
async def test_ephemera_cleared_on_switch_and_rebuild(tmp_path, how):
    """Пересборка и уход в другую сессию с возвратом очищают память:
    снятый ход возвращается как после переоткрытия."""
    from test_turn_line_history import OTHER, _write_thread

    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await live_session(pilot)
        if how == "rebuild":
            scr._rebuild_from_store()
        else:
            _write_thread(tmp_path / "t", OTHER)
            assert scr.attach_active_task(OTHER)
            assert scr.attach_active_task(HISTORY_THREAD, is_resume=True)
        await pilot.pause()
        restored = await _forgotten_and_back(pilot, scr)
        assert scr._ephemera.bang_clear(None) == []
    assert without_ephemeral(restored) == restored
    assert not any("картинка не принята" in t for _c, t, _ in restored)


@pytest.mark.asyncio
async def test_old_turns_drop_ephemera_on_retention(tmp_path, monkeypatch):
    """Ход ушёл за окно удержания (два сжатия): снятый, он возвращается
    без эфемерного и снимка вида — как после переоткрытия (§3.5)."""
    with pinned_snapshot_clock(monkeypatch):
        async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
            scr = await live_session(pilot)
            _expand_views(scr)
            history = scr._feed_history
            history.start_segment()
            history.start_segment()
            scr._ephemera.drop_before(history.retention_start())
            assert scr.forget_turn(0)
            await pilot.pause()
            restore(scr, 0)
            restored = await _settled_dump(pilot)
    assert without_ephemeral(restored) == restored
    assert [r for r in restored if "-expanded" in r[2]] == []
    assert not any("картинка не принята" in t for _c, t, _ in restored)


def test_retention_start_is_first_window_part():
    from gc_tui.services.history_model import HistoryModel

    model = HistoryModel(segment_starts=[0, 4, 9])
    assert model.retention_start() == 4
    assert HistoryModel(segment_starts=[0, 4]).retention_start() == 0


# ---- дифф телом ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_diff_body_restored_when_age_rule_would_compact(monkeypatch):
    """Дифф, показанный телом, возвращается телом, даже если по возрасту
    хода он был бы компактным: в памяти — только отметка."""
    from gc_tui.services import block_edits
    from gc_tui.services.history_render import render_turns
    from gc_tui.services.tool_blocks import BlockEdit
    from gc_tui.widgets.tool_blocks_view import diff_view_key
    from test_history_render import _Feed, _feed, _model

    model = _model("calls")
    records = load_history_fixture("calls").records
    shown_body, aged = (records[i]["payload"]["call_id"] for i in (4, 14))

    def call_edit(_lookup, call_id, *, window):  # noqa: ARG001
        edit = BlockEdit(call_id, "a.py", "Изменён", 1, 1, 3, None, True)
        return edit, True  # по возрасту хода — компактный

    monkeypatch.setattr(block_edits, "call_edit", call_edit)
    ephemera = Ephemera()
    ephemera.note_view(diff_view_key(shown_body), True)
    async with _Feed().run_test(size=_SIZE) as pilot:
        from gc_tui.services.history_render import ChunkOptions, FeedHost

        refs = render_turns(
            FeedHost(_feed(pilot.app)),
            model,
            range(model.turn_count()),
            before=None,
            options=ChunkOptions(),
            ephemera=ephemera,
        )
        compact = {
            key: edit.compact
            for view in refs.block_views.values()
            for key, edit in view._edits.items()
        }
    assert compact[shown_body] is False
    assert compact[aged] is True


@pytest.mark.asyncio
async def test_opened_turns_restore_compaction_body_shown(tmp_path):
    """Ход, загруженный при открытии, с раскрытым телом сводки сжатия —
    снят и возвращён раскрытым (снимок вида и у ходов отрисовщика
    кусков)."""
    from feed_dump import write_history
    from gc_tui.messages import SummaryToggleRequest

    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        write_history(str(tmp_path / "t"), load_history_fixture("compaction"))
        assert scr.attach_active_task(HISTORY_THREAD, is_resume=True)
        await pilot.pause()
        (no, *_rest) = scr._summary_marker_widgets
        scr.on_summary_toggle_request(SummaryToggleRequest(no))
        before = await _settled_dump(pilot)
        assert any(
            "hidden" not in c and "Контекст сохранён" in t
            for _c, t, c in before
        )
        last = scr._feed_history.turn_count() - 1
        first = scr._window_first
        for turn in range(first, last):
            assert scr.forget_turn(turn)
        # Сразу назад: иначе окно само догрузит недостающее сверху.
        for turn in reversed(range(first, last)):
            restore(scr, turn)
        assert await _settled_dump(pilot) == before


@pytest.mark.asyncio
async def test_restored_group_phrase_belongs_to_its_turn():
    """Раскрытие из снимка вида монтирует фразы группы в своём ходе:
    голова следующего хода куска — его первый виджет."""
    from gc_tui.services.history_model import HistoryInputs, build_history
    from gc_tui.services.history_render import (
        ChunkOptions,
        FeedHost,
        render_turns,
    )
    from gc_tui.widgets.user_reply import UserReplyContainer
    from test_history_render import _Feed, _feed

    model = build_history(
        [{"messages": _records(), "compacted_by": None}],
        inputs=HistoryInputs(root=None, injected_fields=()),
    )
    ephemera = Ephemera()
    ephemera.note_view(f"block:{_GROUP_CALL}", True)
    async with _Feed().run_test(size=_SIZE) as pilot:
        refs = render_turns(
            FeedHost(_feed(pilot.app)),
            model,
            range(2),
            before=None,
            options=ChunkOptions(),
            ephemera=ephemera,
        )
        await pilot.pause()
        texts = [t for _c, t, _ in feed_dump(_feed(pilot.app))]
        head = refs.turn_heads[1]
        assert isinstance(head, UserReplyContainer)
        assert head.plain_text == "turn 2 calls=1 light"
    assert "    − свернуть группу" in texts
