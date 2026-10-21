"""↑ — забрать реплику из очереди (спека P3 §2.4, §10 ответ 2): только в
пустом поле с курсором в нём, без палитры, пикера и навигации по истории;
забирается последняя, впущенное ядром не забирается."""

import json
from pathlib import Path

import pytest

pytest.importorskip("textual")

from gc_core.attachments.model import chip_text
from gc_testkit.tui_support import tui_settings
from gc_tui.app import TuiApp
from gc_tui.models import TraceEvent
from gc_tui.services.footer_slot import QUEUE_ADMITTED_HINT
from gc_tui.services.stores.file_store import FileTaskStore
from gc_tui.widgets.command_input import CommandTextArea
from gc_tui.widgets.user_reply import UserReplyContainer
from image_helpers import make_attachment_file

_TID = "w" * 32


class _BusyRunner:
    """Идущий ход: очередь и снятие из неё."""

    def __init__(self, *, admitted: bool = False):
        self.added: list[dict] = []
        self.withdrawn: list[str] = []
        self.events: list[str] = []
        self._admitted = admitted
        self.admitted_ids: set[str] = set()
        self.running = True

    def is_running(self) -> bool:
        return self.running

    def stop(self) -> None:
        self.events.append("stop")
        self.running = False

    def is_child_input_active(self) -> bool:
        return False

    def add_message(self, message: dict) -> None:
        self.added.append(message)

    def withdraw_message(self, msg_id: str) -> bool:
        self.events.append("withdraw")
        self.withdrawn.append(msg_id)
        return not (self._admitted or msg_id in self.admitted_ids)


def _app(threads: Path) -> TuiApp:
    return TuiApp(
        threads_dir=str(threads),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


def _meta_path(threads: Path) -> Path:
    return threads / f"meta_cli_{_TID}.json"


def _activate(app, scr, threads: Path) -> None:
    threads.mkdir(exist_ok=True)
    (threads / f"data_cli_{_TID}.jsonl").write_text("", encoding="utf-8")
    meta = {"thread_id": _TID, "messages_count": 0, "tui": {}}
    _meta_path(threads).write_text(json.dumps(meta), encoding="utf-8")
    scr._active_task_id = _TID
    app.tui_state.active_task_id = _TID
    scr._store = FileTaskStore(threads_dir=str(threads), system="cli")


def _busy(pilot, tmp_path, **kw) -> _BusyRunner:
    _activate(pilot.app, pilot.app.screen, tmp_path / "threads")
    runner = _BusyRunner(**kw)
    pilot.app.active_runner = runner
    return runner


async def _send(pilot, text: str, attachments=()) -> None:
    await pilot.app.screen.on_command_text_area_submitted(
        CommandTextArea.Submitted(value=text, attachments=attachments)
    )
    await pilot.pause()


def _inp(pilot) -> CommandTextArea:
    return pilot.app.screen.query_one("#cmdinput", CommandTextArea)


def _queued(pilot) -> list[UserReplyContainer]:
    trace = pilot.app.screen.query_one("#trace")
    return [r for r in trace.query(UserReplyContainer) if r.state == "queued"]


def _texts(pilot) -> list[str]:
    return [r._text for r in _queued(pilot)]


def _hint_shown(reply: UserReplyContainer) -> bool:
    return reply.query_one(".queue-hint").display


def _pending_records(tmp_path) -> list[dict]:
    meta = json.loads(_meta_path(tmp_path / "threads").read_text("utf-8"))
    return (meta.get("tui") or {}).get("pending_user_messages", [])


def _footer(pilot) -> str:
    return str(pilot.app.screen.query_one("#footer-slot").render())


async def _up(pilot) -> None:
    await pilot.press("up")
    await pilot.pause()


async def _two_queued(pilot, tmp_path, **kw) -> _BusyRunner:
    runner = _busy(pilot, tmp_path, **kw)
    await _send(pilot, "первое")
    await _send(pilot, "второе")
    _inp(pilot).focus()
    await pilot.pause()
    return runner


@pytest.mark.asyncio
async def test_up_takes_the_last_queued(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        runner = await _two_queued(pilot, tmp_path)
        second_id = runner.added[1]["id"]
        await _up(pilot)
        inp = _inp(pilot)
        assert runner.withdrawn == [second_id]
        assert inp.text == "второе"
        assert inp.cursor_location == inp.document.end
        (left,) = _queued(pilot)
        assert left._text == "первое"
        assert _hint_shown(left)
        records = _pending_records(tmp_path)
        assert [r["content"] for r in records] == ["первое"]
        assert (
            pilot.app.screen._pending_user_widgets.get(left.client_id) is left
        )
        assert len(pilot.app.screen._pending_user_widgets) == 1


@pytest.mark.asyncio
async def test_next_up_with_taken_text_does_not_take_another(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        runner = await _two_queued(pilot, tmp_path)
        await _up(pilot)
        await _up(pilot)
        assert len(runner.withdrawn) == 1
        assert _texts(pilot) == ["первое"]


@pytest.mark.asyncio
async def test_after_clearing_the_input_up_takes_the_next(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        runner = await _two_queued(pilot, tmp_path)
        await _up(pilot)
        _inp(pilot).text = ""
        await pilot.pause()
        await _up(pilot)
        assert runner.withdrawn == [m["id"] for m in runner.added[::-1]]
        assert _inp(pilot).text == "первое"
        assert _queued(pilot) == []
        assert _pending_records(tmp_path) == []


@pytest.mark.asyncio
async def test_admitted_message_shows_footer_hint_and_stays(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        runner = await _two_queued(pilot, tmp_path, admitted=True)
        await _up(pilot)
        assert runner.withdrawn == [runner.added[1]["id"]]
        assert _footer(pilot) == QUEUE_ADMITTED_HINT
        assert _texts(pilot) == ["первое", "второе"]
        assert _inp(pilot).text == ""
        assert len(_pending_records(tmp_path)) == 2


@pytest.mark.asyncio
async def test_images_return_with_the_text(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        runner = _busy(pilot, tmp_path)
        att = make_attachment_file(1, tmp_path)
        text = f"глянь {chip_text(1)}"
        await _send(pilot, text, attachments=(att,))
        _inp(pilot).focus()
        await _up(pilot)
        inp = _inp(pilot)
        assert runner.withdrawn == [runner.added[0]["id"]]
        assert inp.text == text
        assert inp.images_in_buffer() == (att,)
        assert inp.cursor_location == inp.document.end


async def _assert_queue_untouched(pilot, runner) -> None:
    await _up(pilot)
    assert runner.withdrawn == []
    assert _texts(pilot) == ["первое", "второе"]


@pytest.mark.asyncio
async def test_up_with_text_in_input_keeps_queue(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        runner = await _two_queued(pilot, tmp_path)
        _inp(pilot).text = "черновик"
        await pilot.pause()
        await _assert_queue_untouched(pilot, runner)


@pytest.mark.asyncio
async def test_up_with_suggestion_ghost_keeps_queue(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        runner = await _two_queued(pilot, tmp_path)
        _inp(pilot).suggestion = "подсказка"
        await pilot.pause()
        await _assert_queue_untouched(pilot, runner)


@pytest.mark.asyncio
async def test_up_with_palette_open_keeps_queue(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        runner = await _two_queued(pilot, tmp_path)
        pilot.app.screen._open_palette(prefix="")
        await pilot.pause()
        await _assert_queue_untouched(pilot, runner)


@pytest.mark.asyncio
async def test_up_with_picker_open_keeps_queue(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        runner = await _two_queued(pilot, tmp_path)
        pilot.app.screen._open_arg_picker(
            options=[("a", "a"), ("b", "b")], prefix=""
        )
        await pilot.pause()
        await _assert_queue_untouched(pilot, runner)


@pytest.mark.asyncio
async def test_up_while_navigating_history_keeps_queue(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        runner = await _two_queued(pilot, tmp_path)
        pilot.app.screen._history_index = 0
        await _assert_queue_untouched(pilot, runner)


@pytest.mark.asyncio
async def test_up_with_focus_in_trace_keeps_queue(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        runner = await _two_queued(pilot, tmp_path)
        pilot.app.screen.query_one("#trace").focus()
        await pilot.pause()
        await _assert_queue_untouched(pilot, runner)


@pytest.mark.asyncio
async def test_up_on_second_line_moves_cursor(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        runner = await _two_queued(pilot, tmp_path)
        inp = _inp(pilot)
        inp.text = "строка\nвторая"
        inp.cursor_location = (1, 3)
        await pilot.pause()
        await _up(pilot)
        assert inp.cursor_location[0] == 0
        assert inp.text == "строка\nвторая"
        assert runner.withdrawn == []


@pytest.fixture
def _skills(monkeypatch):
    from gc_core.tools.skills import registry as sreg
    from gc_core.tools.skills import skill_delivery as sd

    async def _delivery(skill_id, reg):
        return f"DELIVERY[{skill_id}]"

    async def _available(skill_id, reg):
        return True

    monkeypatch.setattr(sreg, "get_skills_registry", lambda cfg: object())
    monkeypatch.setattr(sd, "build_skill_delivery", _delivery)
    monkeypatch.setattr(sd, "skill_is_available", _available)


@pytest.mark.asyncio
async def test_skill_reply_returns_slash_name_and_text(tmp_path, _skills):
    async with _app(tmp_path / "threads").run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "coder"}]
        runner = _busy(pilot, tmp_path)
        await scr._cmd_use_skill("coder", prompt="почини тест")
        await pilot.pause()
        _inp(pilot).focus()
        await _up(pilot)
        assert runner.withdrawn == [runner.added[0]["id"]]
        assert _inp(pilot).text == "/coder почини тест"
        assert _queued(pilot) == []


# --- очередь при остановке и в конце хода -----------------------------------


@pytest.mark.asyncio
async def test_up_without_runner_does_not_crash(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        await _two_queued(pilot, tmp_path)
        pilot.app.active_runner = None
        assert pilot.app.screen._withdraw_last_queued() is False


@pytest.mark.asyncio
async def test_stop_returns_queue_to_input_in_order(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        runner = _busy(pilot, tmp_path)
        att = make_attachment_file(1, tmp_path)
        first = f"глянь {chip_text(1)}"
        await _send(pilot, first, attachments=(att,))
        await _send(pilot, "второе")
        _inp(pilot).focus()
        pilot.app.screen._cmd_stop()
        await pilot.pause()
        inp = _inp(pilot)
        assert runner.running is False
        assert inp.text == f"{first}\n\nвторое"
        assert inp.images_in_buffer() == (att,)
        assert inp.cursor_location == inp.document.end
        assert _queued(pilot) == []
        assert _pending_records(tmp_path) == []
        assert pilot.app.screen._pending_user_widgets == {}
        await _up(pilot)
        assert runner.events == ["withdraw", "withdraw", "stop"]


@pytest.mark.asyncio
async def test_stop_returns_only_withdrawn_admitted_stays(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        runner = await _two_queued(pilot, tmp_path)
        runner.admitted_ids = {runner.added[1]["id"]}
        pilot.app.screen._cmd_stop()
        await pilot.pause()
        pilot.app.screen._poll_active()
        await pilot.pause()
        assert _inp(pilot).text == "первое"
        trace = pilot.app.screen.query_one("#trace")
        (left,) = trace.query(UserReplyContainer)
        assert (left._text, left.state) == ("второе", "pending")
        records = _pending_records(tmp_path)
        assert [r["content"] for r in records] == ["второе"]


@pytest.mark.asyncio
async def test_stop_appends_queue_after_typed_text(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        await _two_queued(pilot, tmp_path)
        _inp(pilot).text = "черновик"
        await pilot.pause()
        pilot.app.screen._cmd_stop()
        await pilot.pause()
        assert _inp(pilot).text == "черновик\n\nпервое\n\nвторое"
        assert _queued(pilot) == []


@pytest.mark.asyncio
async def test_skill_reply_returns_on_stop_with_slash_name(tmp_path, _skills):
    async with _app(tmp_path / "threads").run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "coder"}]
        _busy(pilot, tmp_path)
        await scr._cmd_use_skill("coder", prompt="почини тест")
        await pilot.pause()
        scr._cmd_stop()
        await pilot.pause()
        assert _inp(pilot).text == "/coder почини тест"


def _admit(scr, wire: str, no: int) -> None:
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


@pytest.mark.asyncio
async def test_turn_end_with_all_admitted_restores_nothing(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        scr = pilot.app.screen
        runner = await _two_queued(pilot, tmp_path)
        _admit(scr, "первое", 5)
        _admit(scr, "второе", 6)
        runner.running = False
        scr._poll_active()
        await pilot.pause()
        assert _inp(pilot).text == ""
        trace = scr.query_one("#trace")
        states = [r.state for r in trace.query(UserReplyContainer)]
        assert states == ["normal", "normal"]


@pytest.mark.asyncio
async def test_turn_end_returns_unadmitted_queue_to_input(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        scr = pilot.app.screen
        runner = await _two_queued(pilot, tmp_path)
        _admit(scr, "первое", 5)
        runner.running = False
        scr._poll_active()
        await pilot.pause()
        assert _inp(pilot).text == "второе"
        assert _queued(pilot) == []
        assert _pending_records(tmp_path) == []


@pytest.mark.asyncio
async def test_turn_end_withdraws_from_core_before_returning(tmp_path):
    """Сообщение во время `/compact` ядро держит и после хода: в поле
    оно идёт, только если снято у ядра — иначе уйдёт и следующим
    прогоном."""
    async with _app(tmp_path / "threads").run_test() as pilot:
        scr = pilot.app.screen
        runner = await _two_queued(pilot, tmp_path)
        runner.running = False
        scr._poll_active()
        await pilot.pause()
        assert runner.withdrawn == [m["id"] for m in runner.added]
        assert _inp(pilot).text == "первое\n\nвторое"
        assert _queued(pilot) == []
        assert _pending_records(tmp_path) == []


@pytest.mark.asyncio
async def test_turn_end_not_withdrawn_becomes_pending(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        scr = pilot.app.screen
        runner = await _two_queued(pilot, tmp_path, admitted=True)
        runner.running = False
        scr._poll_active()
        await pilot.pause()
        assert _inp(pilot).text == ""
        trace = scr.query_one("#trace")
        replies = list(trace.query(UserReplyContainer))
        assert [r.state for r in replies] == ["pending", "pending"]
        assert not any(_hint_shown(r) for r in replies)
        assert len(_pending_records(tmp_path)) == 2
        scr._poll_active()
        await pilot.pause()
        assert len(runner.withdrawn) == 2
