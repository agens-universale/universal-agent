"""Пометка «изображение не просмотрено»: очередь пометок, проводка on_trace_note,
строка `└ !` под владельцем на тике опроса (спека P3 §10, ответ 4)."""

import json
import threading
from types import SimpleNamespace

import pytest

from gc_core.vision.notes import TraceNote
from gc_testkit import as_coro
from gc_tui.services.trace_note_sink import TraceNoteSink
from gc_testkit.tui_support import tui_settings


def _note(text: str) -> TraceNote:
    return TraceNote(text, "reply")


def test_drain_returns_the_thread_notes_in_order_and_drops_the_rest():
    """Пометка только живая и только своей ленты: пометки чужого треда (ход, начатый до
    смены сессии) при снятии выбрасываются — ни сейчас, ни после возврата их нет."""
    sink = TraceNoteSink()
    first, foreign, second = _note("первая"), _note("чужая"), _note("вторая")
    sink.push("A", first)
    sink.push("B", foreign)
    sink.push("A", second)
    assert sink.drain("A") == [first, second]
    assert sink.drain("B") == []
    assert sink.drain("A") == []


def test_push_from_many_threads_loses_nothing():
    sink = TraceNoteSink()
    workers = [
        threading.Thread(
            target=lambda i=i: [
                sink.push("A", _note(f"{i}-{k}")) for k in range(100)
            ]
        )
        for i in range(8)
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert sorted(n.text for n in sink.drain("A")) == sorted(
        f"{i}-{k}" for i in range(8) for k in range(100)
    )


def test_the_hook_tags_a_note_with_the_thread_of_its_run():
    """Хук контекста помечает пометку тредом прогона на момент вызова: контекст один на
    приложение, треды у ходов разные."""
    sink = TraceNoteSink()
    ctx = SimpleNamespace(system_id="cli", thread_id="A")
    hook = sink.hook_for(ctx)
    hook(_note("в A"))
    ctx.thread_id = "B"
    in_b = TraceNote("в B", "call", "c1")
    hook(in_b)
    assert sink.drain("B") == [in_b]


def test_sink_keeps_notes_and_choice_events_per_thread():
    """Событие «выбор применён» идёт той же очередью, что пометки: по
    порядку, с тредом своего прогона, чужое выбрасывается."""
    from gc_core.model_choice import ModelChoiceApplied

    sink = TraceNoteSink()
    ctx = SimpleNamespace(system_id="cli", thread_id="A")
    hook = sink.hook_for_choice(ctx)
    note = _note("пометка")
    applied = ModelChoiceApplied("max", "giga-max")
    sink.push("A", note)
    hook(applied)
    ctx.thread_id = "B"
    hook(ModelChoiceApplied("light", "glm-flash"))
    assert sink.drain("A") == [note, applied]
    assert sink.drain("B") == []


def test_executor_build_wires_on_trace_note_to_the_app_sink(
    tmp_path, monkeypatch
):
    from gc_tui.app import TuiApp

    class _Ctx:
        # Двойник RunContext: TuiApp после сборки включает diff-журнал и снимает
        # снапшот MCP — им нужны эти поля (как в test_mcp_boot_snapshot.py).
        os_backend = None
        tools_data: dict = {}
        mcp_status: dict = {}
        logging_path = str(tmp_path / "run.log")
        system_id = "cli"
        thread_id = "t1"

    class _Ex:
        def __init__(self, ctx):
            pass

        def stop_run(self):
            pass

    ctx = _Ctx()
    monkeypatch.setattr(
        "gc_core.config_modules.auth.build_auth_connector", lambda cfg: None
    )
    monkeypatch.setattr(
        "gc_core.run_context.acreate_run_context", as_coro(lambda *a, **k: ctx)
    )
    monkeypatch.setattr("gc_core.agent_executor.AgentExecutor", _Ex)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    app._build_real_executor()
    note = TraceNote("не просмотрено", "reply")
    ctx.on_trace_note(note)
    assert app._trace_note_sink.drain("t1") == [note]
    from gc_core.model_choice import ModelChoiceApplied

    applied = ModelChoiceApplied("max", "giga-max")
    ctx.on_model_choice_applied(applied)
    assert app._trace_note_sink.drain("t1") == [applied]


def _write_thread(threads, tid: str) -> None:
    (threads / f"data_cli_{tid}.jsonl").write_text(
        json.dumps(
            {
                "type": "message",
                "seq": 1,
                "ts": "2026-09-24T10:00:00+00:00",
                "payload": {"id": "u1", "role": "user", "content": "глянь"},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (threads / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "tui": {}}), encoding="utf-8"
    )


def _call_event(no: int, call_id: str, tool_name: str):
    from gc_tui.models import TraceEvent

    return TraceEvent(
        no=no,
        event_ts_utc_iso="",
        kind="call",
        title=tool_name,
        summary=f"{tool_name}()",
        full_payload={},
        call_id=call_id,
        tool_name=tool_name,
    )


def _standalone(scr):
    from gc_tui.widgets.trace_row import TraceRow

    return [
        (r._content, r.has_class("line-warn"))
        for r in scr.query(TraceRow)
        if r._prefix == "! "
    ]


_TID = "a" * 32


async def _drained(pilot, *notes: TraceNote) -> None:
    for note in notes:
        pilot.app._trace_note_sink.push(_TID, note)
    await pilot.pause(0.7)  # тик опроса — 250 мс


def _bodies(bodies) -> str:
    """Тела строк `└` события (без отступа и `└ `)."""
    return "\n".join(b.plain for b in bodies)


def _call_bodies(row) -> str:
    return _bodies(row._consequence_bodies())


def _app(tmp_path):
    from gc_tui.app import TuiApp

    threads = tmp_path / "threads"
    threads.mkdir()
    _write_thread(threads, _TID)
    return TuiApp(
        threads_dir=str(threads), system="cli", settings=tui_settings({})
    )


@pytest.mark.asyncio
async def test_reply_note_sits_under_the_reply_with_images_and_only_live(
    tmp_path,
):
    """Пометка ответа — строкой `└ ! …` (`WARN`) под репликой с картинкой, не
    под последней репликой; отдельной строки нет; чужой тред не рисуется,
    при открытии сессии заново пометки нет."""
    pytest.importorskip("textual")
    from image_helpers import make_attachment

    from gc_core.vision.notes import unsupported_note
    from gc_tui.palette import WARN
    from gc_tui.widgets.user_reply import UserReplyContainer

    note = unsupported_note("glm")[0]
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID)
        await pilot.pause()
        with_image = UserReplyContainer(
            "вот", images=(make_attachment("f" * 64, 1),)
        )
        scr._trace_mount(with_image)
        scr._trace_mount(UserReplyContainer("и ещё"))
        pilot.app._trace_note_sink.push("b" * 32, _note("из другой сессии"))
        await _drained(pilot, TraceNote(note, "reply"))
        replies = list(scr.query(UserReplyContainer))
        # «глянь» из треда, «вот» с картинкой, «и ещё» — последняя.
        assert [_bodies(r._consequences) for r in replies] == [
            "",
            f"! {note}",
            "",
        ]
        assert WARN in {
            str(s.style) for b in with_image._consequences for s in b.spans
        }
        assert _standalone(scr) == []
        scr.attach_active_task(_TID, is_resume=True)
        await pilot.pause()
        assert all(not r._consequences for r in scr.query(UserReplyContainer))


@pytest.mark.parametrize(
    "tone, plain, style",
    [
        ("info", "deepseek не принимает изображения", "DIM"),
        ("warn", "! deepseek не принимает изображения", "WARN"),
    ],
)
@pytest.mark.asyncio
async def test_trace_note_tone_paints_the_reply_consequence(
    tmp_path, tone, plain, style
):
    """Пометка под репликой с картинкой: `info` — серое следствие без `!`,
    `warn` — прежнее оранжевое `! …`."""
    pytest.importorskip("textual")
    from image_helpers import make_attachment

    from gc_tui import palette
    from gc_tui.widgets.user_reply import UserReplyContainer

    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID)
        await pilot.pause()
        reply = UserReplyContainer(
            "вот", images=(make_attachment("f" * 64, 1),)
        )
        scr._trace_mount(reply)
        await _drained(
            pilot,
            TraceNote(
                "deepseek не принимает изображения", "reply", None, tone
            ),
        )
        body = reply._consequences[-1]

    assert body.plain == plain
    assert {str(s.style) for s in body.spans} == {getattr(palette, style)}


@pytest.mark.asyncio
async def test_reply_note_without_images_goes_under_the_latest_reply(
    tmp_path,
):
    pytest.importorskip("textual")
    from gc_tui.widgets.user_reply import UserReplyContainer

    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID)
        await pilot.pause()
        scr._trace_mount(UserReplyContainer("последняя"))
        await _drained(pilot, _note("не просмотрено"))
        last = list(scr.query(UserReplyContainer))[-1]
        assert _bodies(last._consequences) == "! не просмотрено"


@pytest.mark.asyncio
async def test_call_note_sits_under_its_call_row_top_level_and_nested(
    tmp_path,
):
    """Пометка вызова — `└ ! …` под строкой этого вызова: верхнего уровня,
    вложенного (`fs_view_image` субагента) и строки запуска субагента
    (отказ модели субагента вне его тулов)."""
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow

    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID)
        await pilot.pause()
        scr._render_event(_call_event(10, "top", "fs_view_image"))
        scr._render_event(_call_event(11, "sa", "subagent_run"))
        scr._mount_nested_tool(
            parent_call_id="sa",
            tool_name="fs_view_image",
            tool_call_id="kid",
            args_summary="",
        )
        await pilot.pause()
        await _drained(
            pilot,
            TraceNote("сверху", "call", "top"),
            TraceNote("внутри", "call", "kid"),
            TraceNote("субагент", "call", "sa"),
        )
        rows = {r.call_id: r for r in scr.query(ToolCallRow)}
        assert _call_bodies(rows["top"]) == "! сверху"
        assert _call_bodies(rows["kid"]) == "! внутри"
        assert rows["kid"]._level == 1
        assert _call_bodies(rows["sa"]) == "! субагент"
        assert _standalone(scr) == []


def _append_call_record(tmp_path, call_id: str) -> None:
    path = tmp_path / "threads" / f"data_cli_{_TID}.jsonl"
    record = {
        "type": "message",
        "seq": 2,
        "ts": "2026-09-24T10:00:01+00:00",
        "payload": {
            "type": "function_call",
            "id": f"fc_{call_id}",
            "call_id": call_id,
            "name": "fs_view_image",
            "arguments": "{}",
        },
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


@pytest.mark.asyncio
async def test_call_note_arriving_with_its_call_record_lands_under_the_row(
    tmp_path,
):
    """Пометка и запись её вызова пришли в один тик: пометка — под строкой
    вызова, не отдельной строкой (записи тика рисуются раньше пометок)."""
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow

    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID)
        await pilot.pause()
        _append_call_record(tmp_path, "c9")
        await _drained(pilot, TraceNote("не просмотрено", "call", "c9"))
        rows = {r.call_id: r for r in scr.query(ToolCallRow)}
        assert _call_bodies(rows["c9"]) == "! не просмотрено"
        assert _standalone(scr) == []


@pytest.mark.asyncio
async def test_call_note_waits_for_its_row_then_falls_back(tmp_path):
    """Строки вызова ещё нет — пометка ждёт несколько тиков; строка
    появилась — пометка под ней; не появилась — строка `! …` `WARN`."""
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow

    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID)
        await pilot.pause()
        await _drained(
            pilot,
            TraceNote("дождалась", "call", "late"),
            TraceNote("потерялась", "call", "нет-такого"),
        )
        assert _standalone(scr) == []
        _append_call_record(tmp_path, "late")
        for _ in range(40):  # предел ожидания — несколько тиков по 250 мс
            await pilot.pause(0.25)
            if _standalone(scr):
                break
        rows = {r.call_id: r for r in scr.query(ToolCallRow)}
        assert _call_bodies(rows["late"]) == "! дождалась"
        assert _standalone(scr) == [("потерялась", True)]


@pytest.mark.asyncio
async def test_waiting_note_of_another_thread_is_dropped(tmp_path):
    """Пометка ждала строку вызова в треде A, сессию сменили на B: в ленту B
    она не попадает — ни под строкой с тем же call_id, ни строкой `!`."""
    pytest.importorskip("textual")
    from gc_tui.widgets.tool_call_row import ToolCallRow

    other = "c" * 32
    async with _app(tmp_path).run_test() as pilot:
        _write_thread(tmp_path / "threads", other)
        scr = pilot.app.screen
        scr.attach_active_task(_TID)
        await pilot.pause()
        await _drained(pilot, TraceNote("из A", "call", "late"))
        assert scr._waiting_notes
        scr.attach_active_task(other)
        await pilot.pause()
        scr._render_event(_call_event(10, "late", "fs_view_image"))
        await pilot.pause(0.7)
        assert scr._waiting_notes == []
        (row,) = [r for r in scr.query(ToolCallRow) if r.call_id == "late"]
        assert row._consequence_bodies() == []
        assert _standalone(scr) == []
