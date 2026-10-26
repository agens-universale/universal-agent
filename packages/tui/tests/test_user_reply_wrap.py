"""Реплика пользователя в ленте переносится своей раскладкой: плашка картинки целиком,
продолжение — под текстом, а не под `> `. Выделение мышью и двойной клик копируют
текст сообщения — без префиксов, отступов и переносов раскладки."""

import json

import pytest

pytest.importorskip("textual")

from rich.cells import cell_len
from rich.color import Color
from textual.app import App, ComposeResult
from textual.widgets import Static

from gc_core.attachments.model import chip_text
from gc_testkit.tui_support import wait_until
from gc_tui.messages import OpenImageRequest
from gc_tui.palette import DIM
from gc_tui.widgets.user_reply import NestedUserReply, UserReplyContainer
from image_helpers import make_attachment_file
from gc_testkit.tui_support import tui_settings

#: Колонки и подложка реплики — как в styles.tcss приложения (кромка и поля
#: сужают содержимое на 3 ячейки).
_CSS = """
.user-band { border-left: outer red; padding: 0 1 0 1; height: auto; width: 1fr; }
.trace-prefix { width: auto; height: auto; }
.trace-content { width: 1fr; height: auto; }
Static { height: auto; }
"""

AGENT = "ответ агента"
#: Картинка вне проекта и вне домашней папки — путь показывается полностью.
OUTSIDE = "/Users/someone/Desktop/screens/2026/Снимок экрана.png"


class _Host(App):
    """Строка агента над репликой — для протяжки через обе."""

    CSS = _CSS
    AUTO_FOCUS = None

    def __init__(self, widget):
        super().__init__()
        self._widget = widget
        self.images: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static(AGENT, id="agent")
        yield self._widget

    def on_open_image_request(self, message: OpenImageRequest) -> None:
        self.images.append(message.path)


def _lines(widget) -> list[str]:
    return widget.render().plain.split("\n")


def _origin(widget) -> tuple[int, int]:
    """Сдвиг содержимого от угла виджета (кромка и поля)."""
    return (
        widget.content_region.x - widget.region.x,
        widget.content_region.y - widget.region.y,
    )


async def _drag(pilot, start_widget, start, end_widget, end) -> str:
    await pilot.mouse_down(start_widget, offset=start)
    await pilot.hover(end_widget, offset=end)
    await pilot.mouse_up(end_widget, offset=end)
    await wait_until(
        pilot,
        lambda: pilot.app.screen.get_selected_text() is not None,
        "выделение протяжкой",
    )
    return pilot.app.screen.get_selected_text()


def _chip_text_reply(tmp_path):
    att = make_attachment_file(6, tmp_path)
    text = f"посмотри пожалуйста {chip_text(6)} и скажи что там"
    sub_line = f"└ {chip_text(6)} · скрин.png"
    return UserReplyContainer(text, images=(att,)), text, sub_line, att


# ---- перенос ----


@pytest.mark.asyncio
async def test_reply_wraps_under_the_text_and_keeps_chips_whole(tmp_path):
    reply, _text, _sub_line, _att = _chip_text_reply(tmp_path)
    async with _Host(reply).run_test(size=(30, 20)) as pilot:
        await pilot.pause()
        lines = _lines(reply)
        assert reply.content_size.height == len(lines) > 2
        assert all(
            cell_len(line) <= reply.content_size.width for line in lines
        )
        assert lines[0].startswith("> ")
        assert all(line.startswith("  ") for line in lines[1:])
        # плашка в тексте и в строке картинки — обе целиком
        assert sum(chip_text(6) in line for line in lines) == 2


@pytest.mark.asyncio
async def test_reply_rewraps_on_resize():
    reply = UserReplyContainer("слово " * 20)
    async with _Host(reply).run_test(size=(30, 20)) as pilot:
        await pilot.pause()
        narrow = reply.content_size.height
        await pilot.resize_terminal(80, 20)
        await pilot.pause()
        assert reply.content_size.height == len(_lines(reply)) < narrow


@pytest.mark.asyncio
async def test_failed_reply_continues_under_the_text():
    reply = UserReplyContainer("слово " * 10, state="pending")
    async with _Host(reply).run_test(size=(30, 20)) as pilot:
        await pilot.pause()
        reply.set_state("failed")
        await pilot.pause()
        lines = _lines(reply)
        assert lines[0].startswith("[!] > слово")
        assert all(line.startswith(" " * 6 + "слово") for line in lines[1:])
        assert reply.content_size.height == len(lines)


@pytest.mark.asyncio
async def test_chip_on_a_wrapped_row_highlights_and_opens(tmp_path):
    reply, _text, _sub_line, att = _chip_text_reply(tmp_path)
    async with _Host(reply).run_test(size=(30, 20)) as pilot:
        await pilot.pause()
        lines = _lines(reply)
        y = next(
            i
            for i, line in enumerate(lines)
            if i and line.lstrip().startswith(chip_text(6))
        )
        dx, dy = _origin(reply)
        chip = (dx + lines[y].index(chip_text(6)) + 2, dy + y)
        await pilot.hover(reply, offset=chip)
        assert reply._hover_image_id == att.id
        await pilot.click(reply, offset=chip)
        await pilot.pause()
        assert pilot.app.images == [att.path]


@pytest.mark.asyncio
async def test_nested_reply_wraps_under_its_text(tmp_path):
    att = make_attachment_file(3, tmp_path)
    text = f"ответ ребёнку {chip_text(3)} и ещё немного текста"
    row = NestedUserReply(text, images=(att,))
    async with _Host(row).run_test(size=(40, 20)) as pilot:
        await pilot.pause()
        body = row.query_one(".trace-content")
        lines = _lines(body)
        assert body.content_size.height == len(lines) > 2
        assert all(cell_len(line) <= body.content_size.width for line in lines)
        assert sum(chip_text(3) in line for line in lines) == 2


# ---- строка картинки: путь серым, в одну строку ----


def _home_and_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = home / "proj"
    project.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(project)
    return home, project


def test_image_line_shows_the_path_from_the_project_in_grey(
    tmp_path, monkeypatch
):
    _home, project = _home_and_project(tmp_path, monkeypatch)
    source = str(project / "docs" / "shot.png")
    att = make_attachment_file(6, tmp_path, source=source)
    rendered = UserReplyContainer(chip_text(6), images=(att,)).render()
    assert (
        rendered.plain.split("\n")[1] == f"  └ {chip_text(6)} · docs/shot.png"
    )
    path = rendered.plain.index("docs/shot.png")
    assert {
        s.style.color
        for s in rendered.spans
        if s.start <= path < s.end and s.style.color
    } == {Color.parse(DIM)}


def test_image_line_outside_the_project_uses_tilde(tmp_path, monkeypatch):
    home, _project = _home_and_project(tmp_path, monkeypatch)
    att = make_attachment_file(
        6, tmp_path, source=str(home / "Desktop" / "shot.png")
    )
    rendered = UserReplyContainer(chip_text(6), images=(att,)).render()
    assert rendered.plain.split("\n")[1] == (
        f"  └ {chip_text(6)} · ~/Desktop/shot.png"
    )


@pytest.mark.asyncio
async def test_long_path_stays_on_one_row_and_copies_whole(tmp_path):
    att = make_attachment_file(6, tmp_path, source=OUTSIDE)
    reply = UserReplyContainer(chip_text(6), images=(att,))
    async with _Host(reply).run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        lines = _lines(reply)
        assert lines[1:] == [
            f"  └ {chip_text(6)} · /Users/someone/…/Снимок экрана.png"
        ]
        await pilot.resize_terminal(34, 20)
        await pilot.pause()
        # не влезает и `/…/имя` — одно имя (переносится под плашку)
        assert _lines(reply)[1:] == [
            f"  └ {chip_text(6)} · Снимок",
            "    экрана.png",
        ]
        dx, dy = _origin(reply)
        await pilot.click(reply, offset=(dx + 3, dy), times=2)
        await pilot.pause()
        assert pilot.app.screen.get_selected_text() == (
            f"{chip_text(6)}\n└ {chip_text(6)} · {OUTSIDE}"
        )


# ---- копирование ----


@pytest.mark.asyncio
async def test_drag_across_a_wrap_copies_the_message_text():
    text = "один два три четыре пять шесть семь восемь"
    reply = UserReplyContainer(text)
    async with _Host(reply).run_test(size=(24, 20)) as pilot:
        await pilot.pause()
        lines = _lines(reply)
        assert lines == [
            "> один два три четыре",
            "  пять шесть семь",
            "  восемь",
        ]
        dx, dy = _origin(reply)
        selected = await _drag(
            pilot, reply, (dx + 2, dy), reply, (dx + 5, dy + 2)
        )
        assert selected == text[: text.index("восемь") + len("восе")]


@pytest.mark.asyncio
async def test_double_click_copies_the_whole_message(tmp_path):
    reply, text, sub_line, _att = _chip_text_reply(tmp_path)
    async with _Host(reply).run_test(size=(30, 20)) as pilot:
        await pilot.pause()
        dx, dy = _origin(reply)
        await pilot.click(reply, offset=(dx + 3, dy), times=2)
        await pilot.pause()
        assert pilot.app.screen.get_selected_text() == f"{text}\n{sub_line}"


@pytest.mark.asyncio
async def test_drag_from_the_agent_row_into_the_reply():
    """Часть агента копируется как раньше (виджет не тронут), часть реплики — текстом
    сообщения."""
    text = "один два три четыре пять шесть семь восемь"
    reply = UserReplyContainer(text)
    async with _Host(reply).run_test(size=(24, 20)) as pilot:
        await pilot.pause()
        agent = pilot.app.query_one("#agent")
        dx, dy = _origin(reply)
        selected = await _drag(pilot, agent, (0, 0), reply, (dx + 6, dy + 1))
        head, _, tail = selected.partition("\n")
        assert head == AGENT
        assert tail.startswith("один два три четыре пять")
        assert tail in text


@pytest.mark.asyncio
async def test_nested_reply_copies_without_the_marker(tmp_path):
    att = make_attachment_file(3, tmp_path)
    text = f"ответ ребёнку {chip_text(3)} и ещё немного текста"
    row = NestedUserReply(text, images=(att,))
    async with _Host(row).run_test(size=(40, 20)) as pilot:
        await pilot.pause()
        body = row.query_one(".trace-content")
        await pilot.click(body, offset=(1, 0), times=2)
        await pilot.pause()
        assert pilot.app.screen.get_selected_text() == (
            f"{text}\n└ {chip_text(3)} · скрин.png"
        )
        agent = pilot.app.query_one("#agent")
        selected = await _drag(pilot, agent, (0, 0), body, (4, 0))
        assert ">" not in selected


# ---- приложение целиком: автокопия выделения и Ctrl+C ----


def _thread_with_reply(tmp_path, text, att) -> tuple[str, str]:
    threads = tmp_path / "threads"
    threads.mkdir()
    tid = "a" * 32
    record = {
        "type": "message",
        "seq": 1,
        "ts": "2026-09-30T10:00:00+00:00",
        "payload": {
            "id": "u1",
            "role": "user",
            "content": text,
            "attachments": [att.to_dict()],
        },
    }
    (threads / f"data_cli_{tid}.jsonl").write_text(
        json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (threads / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "tui": {}}), encoding="utf-8"
    )
    return str(threads), tid


@pytest.mark.asyncio
async def test_app_copies_the_message_text_of_a_wrapped_reply(
    tmp_path, monkeypatch
):
    """Путь до буфера ОС: протяжка (автокопия) и двойной клик + Ctrl+C; путь
    картинки на экране свёрнут, в копии — целиком."""
    from gc_tui.app import TuiApp

    copied: list[str] = []
    monkeypatch.setattr(
        "gc_tui.app.copy_out", lambda _app, text, **_kw: copied.append(text)
    )
    att = make_attachment_file(8, tmp_path, source=OUTSIDE)
    text = f"{chip_text(8)} глянь"
    threads, tid = _thread_with_reply(tmp_path, text, att)
    app = TuiApp(threads_dir=threads, system="cli", settings=tui_settings({}))
    async with app.run_test(size=(60, 30)) as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)
        reply = pilot.app.screen.query_one(UserReplyContainer)
        lines = _lines(reply)
        assert len(lines) == 2  # строка картинки — в одну строку
        assert lines[1].startswith(f"  └ {chip_text(8)} · /Users/")
        assert lines[1].endswith("/…/Снимок экрана.png")
        sub_line = f"└ {chip_text(8)} · {OUTSIDE}"
        dx, dy = _origin(reply)

        await pilot.click(reply, offset=(dx + len(lines[0]) - 2, dy), times=2)
        await pilot.pause(0.3)
        await pilot.press("ctrl+c")
        await wait_until(pilot, lambda: copied, "копия по Ctrl+C")
        assert copied == [f"{text}\n{sub_line}"]

        copied.clear()
        await pilot.mouse_down(reply, offset=(dx + 2, dy))
        end = (dx + len(lines[1]) - 1, dy + 1)
        await pilot.hover(reply, offset=end)
        await pilot.mouse_up(reply, offset=end)
        await wait_until(pilot, lambda: copied, "автокопия протяжки")
        assert copied == [f"{text}\n{sub_line}"]
