"""Открыть картинку просмотрщиком ОС (дизайн картинок §20.2): в ленте — клик (и Ctrl+клик)
по плашке или её строке под репликой, в поле ввода — Ctrl+клик по плашке; файла нет —
отказ. Наведение на картинку в ленте подсвечивает все её места (смоук 2026-09-25)."""

import json

import pytest

pytest.importorskip("textual")

from rich.style import Style
from textual import events
from textual.app import App, ComposeResult

from gc_core.attachments.model import chip_text
from gc_tui.app import TuiApp
from gc_tui.messages import OpenEventRequest, OpenImageRequest
from gc_tui.widgets.command_input import CommandTextArea
from gc_tui.widgets.user_reply import UserReplyContainer
from image_helpers import make_attachment_file
from gc_testkit.tui_support import tui_settings


def _style(image_id: str | None):
    """Стиль ячейки под курсором — как его кладёт Screen: у спана картинки meta `image_id`."""
    return Style(meta={"image_id": image_id}) if image_id else None


def _click(
    widget,
    *,
    ctrl: bool,
    image_id: str | None = None,
    x: int = 1,
    chain: int = 1,
):
    return events.Click(
        widget=widget,
        x=x,
        y=0,
        delta_x=0,
        delta_y=0,
        button=1,
        shift=False,
        meta=False,
        ctrl=ctrl,
        style=_style(image_id),
        chain=chain,
    )


def _mouse_down(
    widget, *, x: int, ctrl: bool, button: int = 1, image_id: str | None = None
):
    return events.MouseDown(
        widget=widget,
        x=x,
        y=0,
        delta_x=0,
        delta_y=0,
        button=button,
        shift=False,
        meta=False,
        ctrl=ctrl,
        style=_style(image_id),
    )


def _press_and_click(
    reply,
    image_id: str | None,
    *,
    x: int = 1,
    click_image_id="same",
    click_x: int | None = None,
    chain: int = 1,
) -> None:
    """Нажатие и клик, как их шлёт терминал: нажатие в ячейке `x`, клик — там же, если
    не сказано иное (`click_x`, `click_image_id`)."""
    reply.on_mouse_down(_mouse_down(reply, x=x, ctrl=False, image_id=image_id))
    reply.on_click(
        _click(
            reply,
            ctrl=False,
            chain=chain,
            x=x if click_x is None else click_x,
            image_id=image_id if click_image_id == "same" else click_image_id,
        )
    )


class _Recorder(App):
    """Хост одного виджета, записывающий запросы открытия."""

    AUTO_FOCUS = None

    def __init__(self, widget):
        super().__init__()
        self._widget = widget
        self.images: list[str] = []
        self.events: list[int] = []

    def compose(self) -> ComposeResult:
        yield self._widget

    def on_open_image_request(self, message: OpenImageRequest) -> None:
        self.images.append(message.path)

    def on_open_event_request(self, message: OpenEventRequest) -> None:
        self.events.append(message.event_no)


# ---- лента ----


@pytest.mark.asyncio
async def test_ctrl_click_on_a_chip_or_sub_line_opens_the_image(tmp_path):
    att = make_attachment_file(8, tmp_path)
    reply = UserReplyContainer(
        f"{chip_text(8)} глянь", event_no=7, images=(att,)
    )
    async with _Recorder(reply).run_test() as pilot:
        reply.on_click(_click(reply, ctrl=True, image_id=att.id))
        await pilot.pause()
        assert pilot.app.images == [att.path]
        assert pilot.app.events == []


@pytest.mark.asyncio
async def test_ctrl_click_on_plain_reply_text_opens_the_event(tmp_path):
    att = make_attachment_file(8, tmp_path)
    reply = UserReplyContainer(
        f"{chip_text(8)} глянь", event_no=7, images=(att,)
    )
    async with _Recorder(reply).run_test() as pilot:
        reply.on_click(_click(reply, ctrl=True))
        await pilot.pause()
        assert pilot.app.images == []
        assert pilot.app.events == [7]


@pytest.mark.asyncio
async def test_plain_click_started_and_released_on_a_chip_opens_the_image(
    tmp_path,
):
    att = make_attachment_file(8, tmp_path)
    reply = UserReplyContainer(
        f"{chip_text(8)} глянь", event_no=7, images=(att,)
    )
    async with _Recorder(reply).run_test() as pilot:
        _press_and_click(reply, att.id)
        await pilot.pause()
        assert pilot.app.images == [att.path]
        assert pilot.app.events == []


@pytest.mark.asyncio
async def test_a_click_that_did_not_start_on_the_chip_opens_nothing(tmp_path):
    """Протяжка выделения от текста, отпущенная на плашке, — не клик по картинке."""
    att = make_attachment_file(8, tmp_path)
    reply = UserReplyContainer(
        f"{chip_text(8)} глянь", event_no=7, images=(att,)
    )
    async with _Recorder(reply).run_test() as pilot:
        _press_and_click(reply, None, click_image_id=att.id)
        await pilot.pause()
        assert pilot.app.images == []


@pytest.mark.asyncio
async def test_a_drag_inside_one_chip_opens_nothing(tmp_path):
    """Нажатие и отпускание в разных ячейках одной плашки — выделение текста, не клик."""
    att = make_attachment_file(8, tmp_path)
    reply = UserReplyContainer(
        f"{chip_text(8)} глянь", event_no=7, images=(att,)
    )
    async with _Recorder(reply).run_test() as pilot:
        _press_and_click(reply, att.id, x=1, click_x=5)
        await pilot.pause()
        assert pilot.app.images == []


@pytest.mark.asyncio
async def test_the_second_click_of_a_double_click_opens_nothing(tmp_path):
    att = make_attachment_file(8, tmp_path)
    reply = UserReplyContainer(
        f"{chip_text(8)} глянь", event_no=7, images=(att,)
    )
    async with _Recorder(reply).run_test() as pilot:
        _press_and_click(reply, att.id, chain=1)
        _press_and_click(reply, att.id, chain=2)
        await pilot.pause()
        assert pilot.app.images == [att.path]


@pytest.mark.asyncio
async def test_a_plain_click_on_a_chip_under_security_confirm_selects_the_row(
    tmp_path,
):
    """Пока баннер подтверждения ждёт ответа, клик по картинке — как обычный клик: выделяет
    строку (гейт клик-модели), подсветки нет."""
    att = make_attachment_file(8, tmp_path)
    reply = UserReplyContainer(
        f"{chip_text(8)} глянь", event_no=7, images=(att,)
    )
    async with _Recorder(reply).run_test() as pilot:
        pilot.app.screen._confirm_active = True
        reply.on_mouse_move(
            events.MouseMove(
                widget=reply,
                x=1,
                y=0,
                delta_x=0,
                delta_y=0,
                button=0,
                shift=False,
                meta=False,
                ctrl=False,
                style=_style(att.id),
            )
        )
        _press_and_click(reply, att.id)
        await pilot.pause()
        assert pilot.app.images == []
        assert reply._hover_image_id is None
        assert reply.has_focus


def _restored_reply_app(tmp_path):
    """Тред с репликой «плашка + глянь» и одной картинкой, открытый в TUI."""
    threads = tmp_path / "threads"
    threads.mkdir()
    att = make_attachment_file(8, tmp_path)
    tid = "a" * 32
    (threads / f"data_cli_{tid}.jsonl").write_text(
        json.dumps(
            {
                "type": "message",
                "seq": 1,
                "ts": "2026-09-24T10:00:00+00:00",
                "payload": {
                    "id": "u1",
                    "role": "user",
                    "content": f"{chip_text(8)} глянь",
                    "attachments": [att.to_dict()],
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (threads / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "tui": {}}), encoding="utf-8"
    )
    return (
        TuiApp(
            threads_dir=str(threads), system="cli", settings=tui_settings({})
        ),
        tid,
        att,
    )


async def _open_reply(pilot, tid):
    """Реплика и смещения её строк: плашки в тексте и её строки под репликой."""
    pilot.app.screen.attach_active_task(tid)
    await pilot.pause(0.3)
    reply = pilot.app.screen.query_one(UserReplyContainer)
    dx = reply.content_region.x - reply.region.x
    dy = reply.content_region.y - reply.region.y
    chip = (dx + len("> ") + 3, dy)
    sub_line = (dx + len("  └ ") + 3, dy + 1)
    text = (dx + len("> ") + len(chip_text(8)) + 3, dy)
    return reply, chip, sub_line, text


def _record_opened(monkeypatch) -> list[str]:
    opened: list[str] = []
    monkeypatch.setattr(
        "gc_tui.services.os_ops.open_path",
        lambda path: opened.append(path) or True,
    )
    return opened


@pytest.mark.asyncio
async def test_real_ctrl_click_on_the_chip_and_the_sub_line_opens_the_image(
    tmp_path, monkeypatch
):
    """Настоящий клик мышью через Screen: meta спана доходит до Click."""
    opened = _record_opened(monkeypatch)
    app, tid, att = _restored_reply_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        reply, chip, sub_line, _text = await _open_reply(pilot, tid)
        await pilot.click(reply, offset=chip, control=True)
        await pilot.click(reply, offset=sub_line, control=True)
        await pilot.pause()
        assert opened == [att.path, att.path]


@pytest.mark.asyncio
async def test_real_plain_click_on_the_chip_and_the_sub_line_opens_the_image(
    tmp_path, monkeypatch
):
    opened = _record_opened(monkeypatch)
    app, tid, att = _restored_reply_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        reply, chip, sub_line, _text = await _open_reply(pilot, tid)
        await pilot.click(reply, offset=chip)
        await pilot.click(reply, offset=sub_line)
        await pilot.pause()
        assert opened == [att.path, att.path]


@pytest.mark.asyncio
async def test_real_plain_click_on_the_text_selects_the_row_and_opens_nothing(
    tmp_path, monkeypatch
):
    opened = _record_opened(monkeypatch)
    app, tid, _att = _restored_reply_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        reply, _chip, _sub_line, text = await _open_reply(pilot, tid)
        await pilot.click(reply, offset=text)
        await pilot.pause()
        assert opened == []
        assert reply.has_focus


@pytest.mark.asyncio
async def test_real_double_click_on_a_chip_opens_the_image_once(
    tmp_path, monkeypatch
):
    opened = _record_opened(monkeypatch)
    app, tid, att = _restored_reply_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        reply, chip, _sub_line, _text = await _open_reply(pilot, tid)
        await pilot.double_click(reply, offset=chip)
        await pilot.pause()
        assert opened == [att.path]


@pytest.mark.asyncio
async def test_real_drag_from_the_text_released_on_a_chip_opens_nothing(
    tmp_path, monkeypatch
):
    opened = _record_opened(monkeypatch)
    app, tid, _att = _restored_reply_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        reply, chip, _sub_line, text = await _open_reply(pilot, tid)
        await pilot.mouse_down(reply, offset=text)
        await pilot.mouse_up(reply, offset=chip)
        await pilot.pause()
        assert opened == []


@pytest.mark.asyncio
async def test_real_hover_highlights_the_image_and_leaves_with_the_pointer(
    tmp_path, monkeypatch
):
    app, tid, att = _restored_reply_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        reply, chip, sub_line, text = await _open_reply(pilot, tid)
        await pilot.hover(reply, offset=chip)
        assert reply._hover_image_id == att.id
        await pilot.hover(reply, offset=text)
        assert reply._hover_image_id is None
        await pilot.hover(reply, offset=sub_line)
        assert reply._hover_image_id == att.id
        await pilot.hover(pilot.app.screen.query_one("#cmdinput"))
        assert reply._hover_image_id is None


@pytest.mark.asyncio
async def test_hover_highlight_goes_out_when_the_terminal_loses_focus(
    tmp_path, monkeypatch
):
    """Клик открыл картинку, фокус ушёл к просмотрщику — «указатель ушёл» терминал
    уже не пришлёт: подсветка гаснет вместе с фокусом окна (смоук 2026-10-01)."""
    app, tid, att = _restored_reply_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        reply, chip, _sub_line, _text = await _open_reply(pilot, tid)
        await pilot.hover(reply, offset=chip)
        assert reply._hover_image_id == att.id
        pilot.app.app_focus = False
        await pilot.pause()
        assert reply._hover_image_id is None
        # Подписка одна — у приложения: реплики на фокус не подписываются, иначе
        # снятые реплики копились бы в наблюдателях App (ревью 2026-10-01).
        # Хранилище наблюдателей — служебное поле Textual: переименуют — тест должен
        # упасть, а не пройти впустую (подписка экрана на фокус в нём есть всегда).
        watchers = getattr(pilot.app, "__watchers")["app_focus"]
        assert watchers
        assert not [
            node
            for node, _cb in watchers
            if isinstance(node, UserReplyContainer)
        ]


@pytest.mark.asyncio
async def test_hover_goes_out_under_a_modal_screen_too(tmp_path, monkeypatch):
    """Фокус окна ушёл, пока сверху модальный экран: реплика ленты под ним тоже
    гаснет — подписка проходит все экраны стека (третье ревью 2026-10-01)."""
    from textual.screen import ModalScreen

    app, tid, att = _restored_reply_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        reply, _chip, _sub_line, _text = await _open_reply(pilot, tid)
        pilot.app.push_screen(ModalScreen())
        await pilot.pause()
        # подсветка, оставшаяся под модальным экраном
        reply._set_image_hover(att.id)
        pilot.app.app_focus = False
        await pilot.pause()
        assert reply._hover_image_id is None


def test_focus_loss_with_an_empty_screen_stack_does_not_raise(tmp_path):
    app, _tid, _att = _restored_reply_app(tmp_path)
    assert not app.screen_stack
    app.watch_app_focus(False)


@pytest.mark.asyncio
async def test_no_hover_highlight_while_the_terminal_is_not_focused(
    tmp_path, monkeypatch
):
    """Окно без фокуса мышь видит, но первый клик macOS лишь активирует окно —
    подсветка обещала бы открытие одним кликом (смоук 2026-10-01)."""
    app, tid, att = _restored_reply_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        reply, chip, _sub_line, text = await _open_reply(pilot, tid)
        pilot.app.app_focus = False
        await pilot.hover(reply, offset=chip)
        assert reply._hover_image_id is None
        pilot.app.app_focus = True
        await pilot.hover(reply, offset=text)
        await pilot.hover(reply, offset=chip)
        assert reply._hover_image_id == att.id


# ---- вложенный ответ (строка субагента) ----


class _NestedHost(_Recorder):
    """Хост вложенной строки: ширины колонок — как в styles.tcss приложения, и строка
    ниже, куда увести указатель."""

    CSS = ".trace-prefix { width: auto; } .trace-content { width: 1fr; }"

    def compose(self) -> ComposeResult:
        from textual.widgets import Static

        yield self._widget
        yield Static("ниже", id="below")


@pytest.mark.asyncio
async def test_nested_reply_images_highlight_and_open_on_click(tmp_path):
    from gc_tui.widgets.user_reply import NestedUserReply

    att = make_attachment_file(3, tmp_path)
    row = NestedUserReply(f"{chip_text(3)} вот ошибка", images=(att,))
    async with _NestedHost(row).run_test(size=(80, 10)) as pilot:
        prefix = row.query_one(".trace-prefix")
        content = row.query_one(".trace-content")
        await pilot.hover(prefix)
        assert row._hover_image_id is None
        await pilot.hover(content, offset=(3, 0))
        assert row._hover_image_id == att.id
        assert any(
            span.style.meta.get("image_id") == att.id
            and span.style.color is not None
            for span in row._content.spans
        )  # перерисована с подсветкой
        await pilot.hover(pilot.app.query_one("#below"))
        assert row._hover_image_id is None
        await pilot.click(
            content, offset=(3, 1)
        )  # строка картинки под ответом
        await pilot.pause()
        assert pilot.app.images == [att.path]


# ---- превью сессии ----


@pytest.mark.asyncio
async def test_preview_opens_an_image_on_click(tmp_path, monkeypatch):
    """Превью показывает те же реплики — клик по картинке открывает её и там."""
    from gc_tui.screens.preview import PreviewScreen

    opened = _record_opened(monkeypatch)
    att = make_attachment_file(8, tmp_path)
    record = {
        "type": "message",
        "thread_id": "t",
        "system": "cli",
        "seq": 1,
        "ts": "2026-09-24T10:00:00Z",
        "payload": {
            "id": "u1",
            "role": "user",
            "content": f"{chip_text(8)} глянь",
            "attachments": [att.to_dict()],
        },
    }

    class _Host(App):
        def on_mount(self):
            self.push_screen(
                PreviewScreen(
                    segments=[
                        {
                            "index": 0,
                            "messages": [record],
                            "compacted_by": None,
                        }
                    ],
                    thread_id="t",
                    project_path="p",
                )
            )

    async with _Host().run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        reply = pilot.app.screen.query_one(UserReplyContainer)
        dx = reply.content_region.x - reply.region.x
        dy = reply.content_region.y - reply.region.y
        await pilot.click(reply, offset=(dx + len("> ") + 3, dy))
        await pilot.pause()
        assert opened == [att.path]


# ---- поле ввода ----


@pytest.mark.asyncio
async def test_ctrl_click_on_an_input_chip_opens_the_image(tmp_path):
    att = make_attachment_file(8, tmp_path)
    inp = CommandTextArea()
    async with _Recorder(inp).run_test() as pilot:
        inp.text = f"{inp._chips.add_image(att, 8)} глянь на это внимательно"
        await pilot.pause()
        await inp._on_mouse_down(_mouse_down(inp, x=4, ctrl=True))
        await pilot.pause()
        assert pilot.app.images == [att.path]


@pytest.mark.asyncio
async def test_ctrl_right_click_on_an_input_chip_opens_instead_of_pasting(
    tmp_path, monkeypatch
):
    """Часть терминалов отдаёт Ctrl+клик как button 3: проверка плашки стоит раньше вставки."""
    monkeypatch.setattr(
        "gc_tui.services.os_ops.read_clipboard", lambda: "ВСТАВКА"
    )
    monkeypatch.setattr(
        "gc_tui.services.os_ops.read_clipboard_image", lambda: None
    )
    att = make_attachment_file(8, tmp_path)
    inp = CommandTextArea()
    async with _Recorder(inp).run_test() as pilot:
        text = f"{inp._chips.add_image(att, 8)} глянь"
        inp.text = text
        await pilot.pause()
        await inp._on_mouse_down(_mouse_down(inp, x=4, ctrl=True, button=3))
        await pilot.pause()
        assert pilot.app.images == [att.path]
        assert inp.text == text


@pytest.mark.asyncio
async def test_ctrl_click_on_input_text_outside_chips_opens_nothing(tmp_path):
    att = make_attachment_file(8, tmp_path)
    inp = CommandTextArea()
    async with _Recorder(inp).run_test() as pilot:
        inp.text = f"{inp._chips.add_image(att, 8)} глянь на это внимательно"
        await pilot.pause()
        await inp._on_mouse_down(_mouse_down(inp, x=30, ctrl=True))
        await pilot.pause()
        assert pilot.app.images == []


# ---- экран ----


def _screen_app(tmp_path) -> TuiApp:
    return TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({}),
    )


@pytest.mark.asyncio
async def test_open_image_request_opens_an_existing_file(
    tmp_path, monkeypatch
):
    opened: list[str] = []
    monkeypatch.setattr(
        "gc_tui.services.os_ops.open_path",
        lambda path: opened.append(path) or True,
    )
    att = make_attachment_file(8, tmp_path)
    async with _screen_app(tmp_path).run_test() as pilot:
        pilot.app.screen.on_open_image_request(OpenImageRequest(att.path))
        await pilot.pause()
        assert opened == [att.path]


@pytest.mark.asyncio
async def test_open_image_request_for_a_missing_file_is_refused(
    tmp_path, monkeypatch
):
    opened: list[str] = []
    monkeypatch.setattr(
        "gc_tui.services.os_ops.open_path",
        lambda path: opened.append(path) or True,
    )
    async with _screen_app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.on_open_image_request(OpenImageRequest(str(tmp_path / "нет.png")))
        await pilot.pause()
        assert opened == []
        assert (
            str(scr.query_one("#footer-left").render()) == "✗ файл недоступен"
        )


@pytest.mark.asyncio
async def test_open_image_request_is_refused_when_the_viewer_fails(
    tmp_path, monkeypatch
):
    """Файл на месте, просмотрщик не запустился — отказ про просмотрщик, не про файл."""
    monkeypatch.setattr("gc_tui.services.os_ops.open_path", lambda path: False)
    att = make_attachment_file(8, tmp_path)
    async with _screen_app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.on_open_image_request(OpenImageRequest(att.path))
        await pilot.pause()
        assert str(scr.query_one("#footer-left").render()) == (
            "✗ не удалось открыть просмотрщик изображений"
        )
