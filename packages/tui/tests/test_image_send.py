"""Отправка сообщений с картинками: отказ «с командой», ветки хода, ответ на вопрос
агента, pending, история и имя сессии, откат отправки, смена сессии (дизайн §12, §20.3)."""

import json
from pathlib import Path

import pytest
from gc_testkit.develop_files import meta_of

pytest.importorskip("textual")

from gc_core.attachments.model import chip_text, user_label
from gc_testkit.tui_support import tui_settings
from gc_tui.app import TuiApp
from gc_tui.models import TraceEvent
from gc_tui.services.stores.file_store import FileTaskStore
from gc_tui.widgets.command_input import CommandTextArea
from gc_tui.widgets.user_reply import UserReplyContainer
from image_helpers import make_attachment_file


class _StartRunner:
    """Раннер без прогона: запоминает аргументы start."""

    def __init__(self):
        self.starts: list[dict] = []

    def is_running(self) -> bool:
        return False

    def is_child_input_active(self) -> bool:
        return False

    def start(self, **kw) -> bool:
        self.starts.append(kw)
        return True


class _BusyRunner:
    """Идущий прогон: сообщения уходят в его очередь."""

    def __init__(self):
        self.added: list[dict] = []

    def is_running(self) -> bool:
        return True

    def is_child_input_active(self) -> bool:
        return False

    def add_message(self, message: dict) -> None:
        self.added.append(message)


class _AskingRunner:
    """Прогон, ждущий ответа на turn_ask_human."""

    def __init__(self):
        self.submitted: list = []

    def is_running(self) -> bool:
        return True

    def submit_ask_human_reply(self, answer) -> None:
        self.submitted.append(answer)


class _StoppableRunner:
    def is_running(self) -> bool:
        return True

    def produced_output(self) -> bool:
        return False

    def stop(self) -> None:
        pass


def _thread(threads: Path, tid: str, *, image_seq: int = 0) -> None:
    """Пустой тред на диске; image_seq — счётчик номеров картинок в meta.runtime."""
    threads.mkdir(exist_ok=True)
    (threads / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")
    meta: dict = {"thread_id": tid, "messages_count": 0, "tui": {}}
    if image_seq:
        meta["runtime"] = {"image_seq": image_seq}
    (threads / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )


def _pending(threads: Path, tid: str) -> list[dict]:
    meta = json.loads(
        (threads / f"meta_cli_{tid}.json").read_text(encoding="utf-8")
    )
    return (meta.get("tui") or {}).get("pending_user_messages", [])


def _activate(app, scr, threads: Path, tid: str) -> None:
    """Активная задача без attach — как в соседних тестах отправки."""
    scr._active_task_id = tid
    app.tui_state.active_task_id = tid
    scr._store = FileTaskStore(threads_dir=str(threads), system="cli")


def _app(threads: Path) -> TuiApp:
    return TuiApp(
        threads_dir=str(threads),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["/help", "!echo"])
async def test_images_with_a_command_are_refused_and_the_input_is_kept(
    tmp_path, typed
):
    async with _app(tmp_path / "threads").run_test() as pilot:
        scr = pilot.app.screen
        inp = scr.query_one("#cmdinput", CommandTextArea)
        att = make_attachment_file(1, tmp_path)
        inp.text = f"{typed} {inp._chips.add_image(att, 1)}"
        await pilot.pause()
        await scr.on_command_text_area_submitted(
            CommandTextArea.Submitted(value=inp.text, attachments=(att,))
        )
        await pilot.pause()
        assert str(scr.query_one("#footer-slot").render()) == (
            "✗ изображения нельзя отправить вместе с командой"
        )
        assert inp.text == f"{typed} {chip_text(1)}"
        assert inp._chips.images_in_text(inp.text) == [att]


@pytest.mark.asyncio
async def test_new_task_starts_with_images_and_records_them_in_pending(
    tmp_path,
):
    threads = tmp_path / "threads"
    threads.mkdir()
    async with _app(threads).run_test() as pilot:
        scr = pilot.app.screen
        runner = _StartRunner()
        pilot.app.active_runner = runner
        scr._image_seq_issued[None] = 1  # выдано до появления треда
        att = make_attachment_file(1, tmp_path)
        text = f"{chip_text(1)} почему кнопка съехала?"
        await scr.on_command_text_area_submitted(
            CommandTextArea.Submitted(value=text, attachments=(att,))
        )
        await pilot.pause()
        tid = pilot.app.tui_state.active_task_id
        assert runner.starts[0]["query"] == text
        assert runner.starts[0]["attachments"] == (att,)
        assert [r["attachments"] for r in _pending(threads, tid)] == [
            [att.to_dict()]
        ]
        assert list(scr.query(UserReplyContainer))[-1]._images == (att,)
        # Автосозданный тред — тот же, в который ушли картинки: счётчик переходит к нему.
        assert scr._image_seq_issued == {tid: 1}


@pytest.mark.asyncio
async def test_continue_thread_starts_with_an_image_only_message(tmp_path):
    threads = tmp_path / "threads"
    tid = "a" * 32
    _thread(threads, tid)
    async with _app(threads).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause()
        runner = _StartRunner()
        pilot.app.active_runner = runner
        att = make_attachment_file(2, tmp_path)
        await scr.on_command_text_area_submitted(
            CommandTextArea.Submitted(value=chip_text(2), attachments=(att,))
        )
        await pilot.pause()
        assert runner.starts[0]["thread_id"] == tid
        assert runner.starts[0]["attachments"] == (att,)
        assert [r["attachments"] for r in _pending(threads, tid)] == [
            [att.to_dict()]
        ]


@pytest.mark.asyncio
async def test_mid_run_message_carries_attachment_dicts(tmp_path):
    threads = tmp_path / "threads"
    tid = "b" * 32
    _thread(threads, tid)
    async with _app(threads).run_test() as pilot:
        scr = pilot.app.screen
        _activate(pilot.app, scr, threads, tid)
        runner = _BusyRunner()
        pilot.app.active_runner = runner
        att = make_attachment_file(3, tmp_path)
        text = f"ещё {chip_text(3)}"
        await scr.on_command_text_area_submitted(
            CommandTextArea.Submitted(value=text, attachments=(att,))
        )
        await pilot.pause()
        assert runner.added == [
            {
                "role": "user",
                "content": text,
                "id": scr._last_user_message_id,
                "attachments": [att.to_dict()],
            }
        ]


@pytest.mark.asyncio
async def test_mid_run_text_message_has_no_attachments_key(tmp_path):
    threads = tmp_path / "threads"
    tid = "b" * 31 + "c"
    _thread(threads, tid)
    async with _app(threads).run_test() as pilot:
        scr = pilot.app.screen
        _activate(pilot.app, scr, threads, tid)
        runner = _BusyRunner()
        pilot.app.active_runner = runner
        await scr.on_command_text_area_submitted(
            CommandTextArea.Submitted(value="просто текст")
        )
        await pilot.pause()
        assert runner.added == [
            {
                "role": "user",
                "content": "просто текст",
                "id": scr._last_user_message_id,
            }
        ]


@pytest.mark.asyncio
async def test_ask_human_reply_with_images_goes_as_human_answer(tmp_path):
    threads = tmp_path / "threads"
    tid = "c" * 32
    _thread(threads, tid)
    async with _app(threads).run_test() as pilot:
        scr = pilot.app.screen
        _activate(pilot.app, scr, threads, tid)
        runner = _AskingRunner()
        pilot.app.active_runner = runner
        scr._on_ask_human_event(
            TraceEvent(
                no=1,
                event_ts_utc_iso="2026-09-24T10:00:00+00:00",
                kind="ask_human",
                title="ask_human",
                summary="Покажите ошибку?",
                full_payload={},
                message_id="ah_1",
                call_id="call_1",
                tool_name="ask_human",
            ),
            live=True,
        )
        await pilot.pause()
        att = make_attachment_file(4, tmp_path)
        text = f"{chip_text(4)} вот она"
        await scr.on_command_text_area_submitted(
            CommandTextArea.Submitted(value=text, attachments=(att,))
        )
        await pilot.pause()
        [answer] = runner.submitted
        # Класс сверяется по имени: тесты ядра с заглушкой `gc_core.tools` перезагружают
        # модули тулов, и в полном прогоне `HumanAnswer` из импорта при сборке — уже не тот
        # класс, что импортирует экран (dataclass `==` сравнивает и класс).
        assert (type(answer).__name__, answer.text, answer.attachments) == (
            "HumanAnswer",
            text,
            (att,),
        )
        assert [r["attachments"] for r in _pending(threads, tid)] == [
            [att.to_dict()]
        ]
        assert list(scr.query(UserReplyContainer))[-1]._images == (att,)


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["/Users/me/shot", "!важно"])
async def test_question_answer_with_slash_or_bang_keeps_its_images(
    tmp_path, typed
):
    """Пока висит вопрос, текст с «/» (не команда) или «!» — ответ агенту,
    и картинки едут с ним: отказ «картинки с командой» — только командам."""
    threads = tmp_path / "threads"
    tid = "c" * 32
    _thread(threads, tid)
    async with _app(threads).run_test() as pilot:
        scr = pilot.app.screen
        _activate(pilot.app, scr, threads, tid)
        runner = _AskingRunner()
        pilot.app.active_runner = runner
        scr._on_ask_human_event(
            TraceEvent(
                no=1,
                event_ts_utc_iso="2026-09-24T10:00:00+00:00",
                kind="ask_human",
                title="ask_human",
                summary="Где ошибка?",
                full_payload={},
                message_id="ah_1",
                call_id="call_1",
                tool_name="ask_human",
            ),
            live=True,
        )
        await pilot.pause()
        att = make_attachment_file(4, tmp_path)
        text = f"{typed} {chip_text(4)}"
        await scr.on_command_text_area_submitted(
            CommandTextArea.Submitted(value=text, attachments=(att,))
        )
        await pilot.pause()
        [answer] = runner.submitted
        assert (answer.text, answer.attachments) == (text, (att,))


@pytest.mark.asyncio
async def test_send_resets_image_chips(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        scr = pilot.app.screen
        pilot.app.active_runner = _StartRunner()
        inp = scr.query_one("#cmdinput", CommandTextArea)
        att = make_attachment_file(1, tmp_path)
        chip = inp._chips.add_image(att, 1)
        inp.text = chip
        await pilot.pause()
        await scr.on_command_text_area_submitted(
            CommandTextArea.Submitted(value=chip, attachments=(att,))
        )
        await pilot.pause()
        assert inp.text == ""
        assert inp._chips.images_in_text(chip) == []


@pytest.mark.asyncio
async def test_history_and_session_name_skip_image_chips(tmp_path):
    threads = tmp_path / "threads"
    tid = "d" * 32
    _thread(threads, tid)
    async with _app(threads).run_test() as pilot:
        scr = pilot.app.screen
        _activate(pilot.app, scr, threads, tid)
        att = make_attachment_file(8, tmp_path)
        scr._on_user_text_submitted(
            f"{chip_text(8)} почему кнопка «Войти» съехала вправо?", (att,)
        )
        assert scr._history.prev(None) == (
            0,
            "почему кнопка «Войти» съехала вправо?",
        )
        assert meta_of(scr._store.get_record(tid))["tui"]["last_actual"] == (
            "почему кнопка «Войти» съехала вправо?"
        )


@pytest.mark.asyncio
async def test_image_only_message_leaves_history_and_session_name_alone(
    tmp_path,
):
    threads = tmp_path / "threads"
    tid = "d" * 31 + "e"
    _thread(threads, tid)
    async with _app(threads).run_test() as pilot:
        scr = pilot.app.screen
        _activate(pilot.app, scr, threads, tid)
        scr._on_user_text_submitted(
            chip_text(8), (make_attachment_file(8, tmp_path),)
        )
        assert scr._history.size == 0
        assert "last_actual" not in (
            meta_of(scr._store.get_record(tid)).get("tui") or {}
        )


@pytest.mark.asyncio
async def test_cancel_with_undo_restores_text_and_image_chips(tmp_path):
    async with _app(tmp_path / "threads").run_test() as pilot:
        scr = pilot.app.screen
        pilot.app.active_runner = _StoppableRunner()
        att = make_attachment_file(8, tmp_path)
        text = f"{chip_text(8)} глянь"
        scr._on_user_text_submitted(text, (att,))
        scr._last_user_message_id = "m_undo"
        scr._trace_mount(UserReplyContainer(text, event_no=0, images=(att,)))
        await pilot.pause()
        scr._cmd_stop()
        await pilot.pause()
        inp = scr.query_one("#cmdinput", CommandTextArea)
        assert inp.text == text
        assert inp._chips.images_in_text(inp.text) == [att]


@pytest.mark.asyncio
async def test_session_switch_renumbers_image_chips_into_the_thread_sequence(
    tmp_path,
):
    threads = tmp_path / "threads"
    tid = "e" * 32
    _thread(threads, tid, image_seq=40)
    async with _app(threads).run_test() as pilot:
        scr = pilot.app.screen
        inp = scr.query_one("#cmdinput", CommandTextArea)
        a1, a10 = (
            make_attachment_file(1, tmp_path),
            make_attachment_file(10, tmp_path),
        )
        inp.text = f"{inp._chips.add_image(a1, 1)} {inp._chips.add_image(a10, 10)} сравни"
        await pilot.pause()
        scr.attach_active_task(tid, is_resume=True)
        await pilot.pause()
        assert inp.text == f"{chip_text(41)} {chip_text(42)} сравни"
        images = inp._chips.images_in_text(inp.text)
        assert [a.label for a in images] == [user_label(41), user_label(42)]
        assert [a.id for a in images] == [a1.id, a10.id]
        assert scr._image_seq_issued[tid] == 42


@pytest.mark.asyncio
async def test_new_session_renumbers_image_chips_from_one(tmp_path):
    threads = tmp_path / "threads"
    tid = "f" * 32
    _thread(threads, tid, image_seq=7)
    async with _app(threads).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause()
        inp = scr.query_one("#cmdinput", CommandTextArea)
        inp.text = f"{inp._chips.add_image(make_attachment_file(8, tmp_path), 8)} текст"
        await pilot.pause()
        scr._cmd_new()
        await pilot.pause()
        assert inp.text == f"{chip_text(1)} текст"
        assert scr._image_seq_issued[None] == 1


@pytest.mark.asyncio
async def test_session_switch_drops_image_records_whose_chips_left_the_text(
    tmp_path,
):
    """Стёртая плашка (запись хранится для Ctrl+Z) номер в новом треде не занимает."""
    threads = tmp_path / "threads"
    tid = "d" * 32
    _thread(threads, tid, image_seq=40)
    async with _app(threads).run_test() as pilot:
        scr = pilot.app.screen
        inp = scr.query_one("#cmdinput", CommandTextArea)
        a1, a2 = (
            make_attachment_file(1, tmp_path),
            make_attachment_file(2, tmp_path),
        )
        inp.text = f"{inp._chips.add_image(a1, 1)} текст"
        inp._chips.add_image(a2, 2)  # плашку #2 стёрли — запись осталась
        await pilot.pause()
        scr.attach_active_task(tid, is_resume=True)
        await pilot.pause()
        assert inp.text == f"{chip_text(41)} текст"
        assert [a.id for a in inp.images_in_buffer()] == [a1.id]
        assert inp._chips.images_in_text(
            f"{chip_text(41)} {chip_text(42)}"
        ) == [inp.images_in_buffer()[0]]
        assert scr._image_seq_issued[tid] == 41


@pytest.mark.asyncio
async def test_session_switch_renumbers_overlapping_and_out_of_order_chips(
    tmp_path,
):
    """#1, #2 → #2, #3 (новый номер одной плашки — старый другой), плашки в тексте не по
    порядку: текст переписан одним проходом, и каждая подпись ведёт к своей картинке."""
    threads = tmp_path / "threads"
    tid = "c" * 32
    _thread(threads, tid, image_seq=1)
    async with _app(threads).run_test() as pilot:
        scr = pilot.app.screen
        inp = scr.query_one("#cmdinput", CommandTextArea)
        a, b = (
            make_attachment_file(1, tmp_path),
            make_attachment_file(2, tmp_path),
        )
        chip_a, chip_b = inp._chips.add_image(a, 1), inp._chips.add_image(b, 2)
        inp.text = f"{chip_b} потом {chip_a}"
        await pilot.pause()
        scr.attach_active_task(tid, is_resume=True)
        await pilot.pause()
        assert inp.text == f"{chip_text(3)} потом {chip_text(2)}"
        assert [(x.id, x.label) for x in inp.images_in_buffer()] == [
            (b.id, user_label(3)),
            (a.id, user_label(2)),
        ]
        assert scr._image_seq_issued[tid] == 3
