"""Очередь (спека P3 §2.4): реплика, отправленная во время хода, — в
состоянии `queued` у низа ленты; всё новое монтируется над очередью,
подсказка — только под последней ожидающей, таймер хода не сбрасывается."""

import json
from pathlib import Path

import pytest

pytest.importorskip("textual")

from rich.color import Color as RichColor
from rich.style import Style
from rich.text import Text
from textual.color import Color

from gc_core.execution.message_keys import companion_message_id
from gc_testkit.tui_support import tui_settings
from gc_tui import palette
from gc_tui.app import TuiApp
from gc_tui.models import TraceEvent
from gc_tui.palette import DIM, MUTED, QUEUE_BG
from gc_tui.services.stores.file_store import FileTaskStore
from gc_tui.services.trace_mapper import map_message
from gc_tui.widgets.command_input import CommandTextArea
from gc_tui.widgets.plan_panel import PlanPanel
from gc_tui.widgets.tool_call_row import ToolCallRow
from gc_tui.widgets.user_reply import QUEUE_HINT, UserReplyContainer

_TID = "q" * 32


class _BusyRunner:
    """Идущий ход: сообщения уходят в его очередь."""

    def __init__(self, *, child: bool = False):
        self.added: list[dict] = []
        self._child = child

    def is_running(self) -> bool:
        return True

    def is_child_input_active(self) -> bool:
        return self._child

    def add_message(self, message: dict) -> None:
        self.added.append(message)

    def withdraw_message(self, msg_id: str) -> bool:
        return True


def _app(threads: Path) -> TuiApp:
    return TuiApp(
        threads_dir=str(threads),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


def _activate(app, scr, threads: Path) -> None:
    """Активная задача с пустым тредом на диске, без attach."""
    threads.mkdir(exist_ok=True)
    (threads / f"data_cli_{_TID}.jsonl").write_text("", encoding="utf-8")
    meta = {"thread_id": _TID, "messages_count": 0, "tui": {}}
    (threads / f"meta_cli_{_TID}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    scr._active_task_id = _TID
    app.tui_state.active_task_id = _TID
    scr._store = FileTaskStore(threads_dir=str(threads), system="cli")


async def _busy(pilot, tmp_path, **kw) -> _BusyRunner:
    scr = pilot.app.screen
    _activate(pilot.app, scr, tmp_path / "threads")
    runner = _BusyRunner(**kw)
    pilot.app.active_runner = runner
    return runner


async def _send(pilot, text: str) -> None:
    await pilot.app.screen.on_command_text_area_submitted(
        CommandTextArea.Submitted(value=text)
    )
    await pilot.pause()


def _trace(pilot):
    return pilot.app.screen.query_one("#trace")


def _replies(pilot) -> list[UserReplyContainer]:
    return list(_trace(pilot).query(UserReplyContainer))


def _hint_shown(reply: UserReplyContainer) -> bool:
    return reply.query_one(".queue-hint").display


def _admit(scr, wire: str, no: int) -> None:
    """Ядро впустило реплику: запись `user` в треде."""
    scr._render_event(
        TraceEvent(
            no=no,
            event_ts_utc_iso="2026-10-21T10:00:00+00:00",
            kind="user",
            title="user",
            summary=wire,
            full_payload={"role": "user", "content": wire},
        )
    )


def _call(scr, call_id: str, no: int) -> None:
    payload = {
        "id": f"f{no}",
        "type": "function_call",
        "name": "fs_read_file",
        "call_id": call_id,
        "arguments": json.dumps({"path": "README.md"}),
    }
    scr._render_event(map_message(payload, no=no, ts=""))


def _color_at(text: Text, index: int):
    styles = (
        Style.parse(s.style) if isinstance(s.style, str) else s.style
        for s in text.spans
        if s.start <= index < s.end
    )
    return {style.color for style in styles if style.color}


# --- палитра -----------------------------------------------------------------


def _between(low: str, mid: str, high: str) -> bool:
    lo, md, hi = (palette._rgb(c) for c in (low, mid, high))
    return all(min(a, c) <= b <= max(a, c) for a, b, c in zip(lo, md, hi))


def test_queue_bg_between_bg_and_user_band_fixed():
    assert palette.QUEUE_BG == palette._mix(
        palette.BG, palette.USER_BAND_BG, palette._QUEUE_SHARE
    )
    assert _between(palette.BG, palette.QUEUE_BG, palette.USER_BAND_BG)
    assert palette.css_variables()["queue-bg"] == palette.QUEUE_BG


def test_queue_bg_recomputed_in_auto_theme():
    try:
        palette.apply_background((0x21, 0x27, 0x33))
        assert palette.QUEUE_BG == palette._mix(
            "#212733", palette.USER_BAND_BG, palette._QUEUE_SHARE
        )
        assert _between(palette.BG, palette.QUEUE_BG, palette.USER_BAND_BG)
        assert palette.css_variables()["queue-bg"] == palette.QUEUE_BG
    finally:
        palette.reset()


# --- виджет ------------------------------------------------------------------


def test_queued_marker_muted():
    reply = UserReplyContainer("и добавь тест", state="queued")
    rendered = reply._band.render()
    assert rendered.plain == "› и добавь тест"
    assert _color_at(rendered, 0) == {RichColor.parse(MUTED)}
    assert reply.has_class("-queued")


def _skill_color(reply):
    return _color_at(reply._band.render(), 3)


def test_queued_reply_shows_full_skill_id_in_reply_colour():
    reply = UserReplyContainer(
        "текст", state="queued", skill="supercoder:brainstorming"
    )
    line = reply.quiet_line()
    assert line.plain == "› /supercoder:brainstorming текст"
    assert _color_at(line, 3) == {RichColor.parse(palette.QUIET_REPLY_INK)}
    assert reply._band.render().plain == ("› /supercoder:brainstorming текст")
    # имя скилла — цветом текста тихой реплики, как и весь её текст
    assert _skill_color(reply) == {RichColor.parse(palette.QUIET_REPLY_INK)}


def test_normal_reply_skill_name_in_skill_colour():
    for state in ("normal", "pending", "failed"):
        reply = UserReplyContainer("текст", state=state, skill="p:coder")
        assert _skill_color(reply) == {RichColor.parse(palette.SKILL)}


def test_missing_skill_name_in_reply_colour():
    reply = UserReplyContainer("текст", skill="coder", skill_error=True)
    assert _skill_color(reply) == set()


def test_skill_without_pack_shows_name():
    reply = UserReplyContainer("текст", skill="computer-use")
    assert reply._band.render().plain == "› /computer-use текст"


def test_skill_error_name_as_typed_in_plain_colour():
    reply = UserReplyContainer(
        "текст", state="queued", skill="coder", skill_error=True
    )
    assert reply.quiet_line().plain == "› /coder текст"
    assert _skill_color(reply) == {RichColor.parse(palette.QUIET_REPLY_INK)}
    assert reply._consequences[0].plain.startswith("скилл coder не найден")


def test_queued_to_normal_drops_class_and_hint():
    reply = UserReplyContainer("x", state="queued")
    reply.set_queue_hint(True)
    reply.set_state("normal")
    assert not reply.has_class("-queued")
    assert not reply.has_class("-queue-hint")


@pytest.mark.asyncio
async def test_queued_look_band_hint_and_copy(tmp_path):
    reply = UserReplyContainer("и добавь тест", state="queued")
    reply.set_queue_hint(True)
    async with _app(tmp_path).run_test(size=(80, 30)) as pilot:
        await _trace(pilot).mount(reply)
        await pilot.pause()
        band = reply.query_one(".user-band")
        hint = reply.query_one(".queue-hint")
        assert band.styles.background == Color.parse(QUEUE_BG)
        assert band.styles.color == Color.parse(DIM)
        assert hint.display
        assert hint.region.y == band.region.bottom
        assert hint.content_region.x == band.content_region.x + 2
        assert hint.styles.color == Color.parse(MUTED)
        assert hint.styles.background != Color.parse(QUEUE_BG)
        assert str(hint.render()) == QUEUE_HINT
        assert hint.get_selection(None) is None


# --- экран: отправка во время хода ------------------------------------------


@pytest.mark.asyncio
async def test_send_during_run_is_queued_last_with_hint(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        runner = await _busy(pilot, tmp_path)
        await _send(pilot, "и добавь тест на ретраи")
        (reply,) = _replies(pilot)
        assert reply.state == "queued"
        assert _trace(pilot).children[-1] is reply
        assert _hint_shown(reply)
        assert reply.message_id == runner.added[0]["id"]
        assert reply.plain_text == "и добавь тест на ретраи"
        assert reply.client_id in pilot.app.screen._pending_user_widgets


@pytest.mark.asyncio
async def test_second_send_moves_hint_to_the_last(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        await _busy(pilot, tmp_path)
        await _send(pilot, "первое")
        await _send(pilot, "второе")
        first, second = _replies(pilot)
        assert (first.state, second.state) == ("queued", "queued")
        assert not _hint_shown(first)
        assert _hint_shown(second)


@pytest.mark.asyncio
async def test_new_rows_mount_above_the_queue(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        await _busy(pilot, tmp_path)
        await _send(pilot, "первое")
        await _send(pilot, "второе")
        _call(scr, "c1", 1)
        scr._mount_trace_row(
            prefix="· ", content="ответ команды", kind_class="line-meta"
        )
        scr._mount_standalone("предупреждение", "warn")
        await pilot.pause()
        kids = list(_trace(pilot).children)
        first, second = _replies(pilot)
        assert kids[-2:] == [first, second]
        assert any(
            isinstance(w, ToolCallRow) and w.call_id == "c1" for w in kids[:-2]
        )
        assert _hint_shown(second) and not _hint_shown(first)


@pytest.mark.asyncio
async def test_anchors_nested_under_parent_plan_above_queue(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        await _busy(pilot, tmp_path)
        parent = ToolCallRow(call_id="p1", label="Agent", target="")
        scr._trace_mount(parent)
        scr._active_calls["p1"] = parent
        await pilot.pause()
        await _send(pilot, "первое")
        await _send(pilot, "второе")
        scr._mount_nested_tool(
            parent_call_id="p1",
            tool_name="fs_read_file",
            tool_call_id="n1",
            args_summary="x",
        )
        scr._ensure_plan_panel_last({"text": "1. шаг", "status": "proposed"})
        await pilot.pause()
        kids = list(_trace(pilot).children)
        first, second = _replies(pilot)
        nested = next(
            w for w in kids if isinstance(w, ToolCallRow) and w.call_id == "n1"
        )
        panel = next(w for w in kids if isinstance(w, PlanPanel))
        # Под `Agent` — блок субагента: заголовок `Explored`, затем член.
        assert kids[kids.index(parent) + 1].mode == "header"
        assert kids.index(nested) == kids.index(parent) + 2
        assert kids.index(panel) < kids.index(first)
        assert kids[-2:] == [first, second]
        # Рамка уже последняя над очередью — повторный вызов копии не делает.
        scr._ensure_plan_panel_last({"text": "1. шаг", "status": "proposed"})
        await pilot.pause()
        assert len(list(_trace(pilot).query(PlanPanel))) == 1


@pytest.mark.asyncio
async def test_nested_fallback_mount_stays_above_queue(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        await _busy(pilot, tmp_path)
        parent = ToolCallRow(call_id="p1", label="Agent", target="")
        scr._trace_mount(parent)
        scr._active_calls["p1"] = parent
        await pilot.pause()
        await _send(pilot, "первое")
        # Якорь уже снят с ленты — монтирование after= падает.
        parent.remove()
        await pilot.pause()
        scr._mount_nested_tool(
            parent_call_id="p1",
            tool_name="fs_read_file",
            tool_call_id="n1",
            args_summary="x",
        )
        await pilot.pause()
        (first,) = _replies(pilot)
        assert list(_trace(pilot).children)[-1] is first


@pytest.mark.asyncio
async def test_admitted_becomes_normal_in_place(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        await _busy(pilot, tmp_path)
        await _send(pilot, "первое")
        await _send(pilot, "второе")
        _admit(scr, "первое", 5)
        await pilot.pause()
        first, second = _replies(pilot)
        assert first.state == "normal"
        assert second.state == "queued"
        assert not _hint_shown(first) and _hint_shown(second)
        _call(scr, "c2", 6)
        await pilot.pause()
        kids = list(_trace(pilot).children)
        call = next(
            w for w in kids if isinstance(w, ToolCallRow) and w.call_id == "c2"
        )
        assert kids.index(first) < kids.index(call) < kids.index(second)
        assert kids[-1] is second


@pytest.mark.asyncio
async def test_run_failure_marks_queued_failed(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        await _busy(pilot, tmp_path)
        await _send(pilot, "первое")
        scr._mark_pending_failed()
        await pilot.pause()
        (reply,) = _replies(pilot)
        assert reply.state == "failed"
        assert not _hint_shown(reply)


@pytest.mark.asyncio
async def test_queue_send_keeps_the_turn_timer(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        await _busy(pilot, tmp_path)
        scr._thinking_started_at = 123.0
        await _send(pilot, "первое")
        assert scr._thinking_started_at == 123.0


@pytest.mark.asyncio
async def test_idle_pending_looks_normal_without_hint(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        reply = scr._mount_user_reply("x", state="pending", client_id="c")
        await pilot.pause()
        assert not reply.has_class("-queued")
        assert not _hint_shown(reply)


@pytest.mark.asyncio
async def test_child_input_draws_no_parent_reply(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        runner = await _busy(pilot, tmp_path, child=True)
        await _send(pilot, "субагенту")
        assert _replies(pilot) == []
        assert runner.added[0]["content"] == "субагенту"


@pytest.mark.asyncio
async def test_attachment_footer_gets_companion_id(tmp_path):
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        runner = await _busy(pilot, tmp_path)
        scr._build_attachment_technical = lambda text: "FOOTER"
        await _send(pilot, "посмотри @notes.md")
        message, footer = runner.added
        assert footer == {
            "role": "user",
            "content": "FOOTER",
            "id": companion_message_id(message["id"]),
        }


# --- скилл во время хода -----------------------------------------------------


def _as_coro(fn):
    async def _wrapped(*a, **kw):
        return fn(*a, **kw)

    return _wrapped


@pytest.fixture
def _skills(monkeypatch):
    from gc_core.tools.skills import registry as sreg
    from gc_core.tools.skills import skill_delivery as sd

    monkeypatch.setattr(sreg, "get_skills_registry", lambda cfg: object())
    monkeypatch.setattr(
        sd,
        "build_skill_delivery",
        _as_coro(lambda skill_id, reg: f"DELIVERY[{skill_id}]"),
    )
    monkeypatch.setattr(
        sd, "skill_is_available", _as_coro(lambda skill_id, reg: True)
    )


@pytest.mark.asyncio
async def test_mid_run_skill_with_text_is_queued(tmp_path, _skills):
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "coder"}]
        runner = await _busy(pilot, tmp_path)
        scr._thinking_started_at = 7.0
        await scr._cmd_use_skill("coder", prompt="почини тест")
        await pilot.pause()
        (reply,) = _replies(pilot)
        assert reply.state == "queued"
        assert reply._band.render().plain == "› /coder почини тест"
        assert reply.plain_text == "/coder почини тест"
        assert reply.message_id == runner.added[0]["id"]
        assert _hint_shown(reply)
        assert scr._thinking_started_at == 7.0


@pytest.mark.asyncio
async def test_mid_run_skill_without_text_is_queued(tmp_path, _skills):
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "coder"}]
        runner = await _busy(pilot, tmp_path)
        await scr._cmd_use_skill("coder")
        await pilot.pause()
        (reply,) = _replies(pilot)
        assert reply.state == "queued"
        assert reply._band.render().plain == "› /coder"
        assert reply.plain_text == "/coder"
        (message,) = runner.added
        assert message["content"] == "DELIVERY[coder]"
        assert message["id"] and reply.message_id == message["id"]


@pytest.mark.asyncio
async def test_list_opens_between_queue_and_input(tmp_path):
    """Очередь — хвост ленты: список из поля встаёт под ней, прямо над
    вводом; ввод на месте, очередь видна над списком."""
    async with _app(tmp_path).run_test(size=(80, 30)) as pilot:
        scr = pilot.app.screen
        await _busy(pilot, tmp_path)
        await _send(pilot, "и добавь тест")
        await pilot.pause(0.4)
        input_y = scr.query_one("#input-row").region.y
        scr._open_palette(prefix="")
        await pilot.pause(0.4)
        (reply,) = _replies(pilot)
        palette = scr._palette.region
        assert scr.query_one("#input-row").region.y == input_y
        assert palette.bottom == input_y
        assert reply.region.height > 0
        assert reply.region.bottom <= palette.y


# --- тихая отметка смены модели у низа (спека model-knob §6.4) ---------------


def _pending_mark(pilot):
    from gc_tui.widgets.model_mark import ModelMark

    (mark,) = _trace(pilot).query(ModelMark)
    assert mark.pending
    return mark


@pytest.mark.asyncio
async def test_pending_mark_and_queued_reply_keep_action_order(tmp_path):
    """Тихая отметка и реплики очереди — в порядке действий пользователя;
    новые события встают над первой из них."""
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        await _busy(pilot, tmp_path)
        await _send(pilot, "первое")
        scr._mark_model_choice("max")
        await pilot.pause()
        await _send(pilot, "второе")
        _call(scr, "c1", 1)
        await pilot.pause()
        first, second = _replies(pilot)
        mark = _pending_mark(pilot)
        kids = list(_trace(pilot).children)
        assert kids[-3:] == [first, mark, second]
        assert isinstance(kids[-4], ToolCallRow)
        assert _hint_shown(second) and not _hint_shown(first)


@pytest.mark.asyncio
async def test_arrow_up_still_withdraws_reply_with_pending_mark(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        runner = await _busy(pilot, tmp_path)
        await _send(pilot, "первое")
        scr._mark_model_choice("max")
        inp = scr.query_one("#cmdinput", CommandTextArea)
        inp.focus()
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert inp.text == "первое"
        assert _replies(pilot) == []
        assert _pending_mark(pilot) is not None
        assert runner.added[0]["id"]


@pytest.mark.asyncio
async def test_applied_mark_rises_above_queued_replies(tmp_path):
    """Применённая отметка уходит над всей очередью: реплики под и над ней
    остаются хвостом у ↑."""
    from gc_core.model_choice import ModelChoiceApplied

    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        await _busy(pilot, tmp_path)
        await _send(pilot, "первое")
        scr._mark_model_choice("max")
        await pilot.pause()
        await _send(pilot, "второе")
        mark = _pending_mark(pilot)
        pilot.app._trace_note_sink.push(_TID, ModelChoiceApplied("max", "M"))
        scr._drain_trace_notes()
        await pilot.pause()
        first, second = _replies(pilot)
        assert list(_trace(pilot).children)[-3:] == [mark, first, second]
        assert not mark.pending
        assert scr._queued_tail(_trace(pilot)) == [first, second]
