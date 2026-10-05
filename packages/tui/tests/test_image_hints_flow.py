"""Подсказки картинок в футере (визуальный контракт картинок §2): совет вставить картинку
из буфера — при возвращении в окно терминала и на старте, один раз на содержимое буфера;
совет про Ctrl+клик — один раз за запуск, после первой вставленной картинки. Буфер ОС
подменён: настоящий буфер разработчика тесты не читают."""

import io
import threading
from pathlib import Path

import pytest
from PIL import Image
from textual import events
from textual.screen import ModalScreen
from textual.widgets import Static

from gc_tui.app import TuiApp
from gc_tui.services import image_hints, os_ops
from gc_tui.widgets.command_input import CommandTextArea
from gc_testkit.tui_support import tui_settings

TIP = "в буфере изображение · Ctrl+V — вставить"


def _png(color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, format="PNG")
    return buf.getvalue()


def _app(tmp_path) -> TuiApp:
    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "paths.threads_data_path": str(tmp_path)}
        ),
    )


class _Clipboard:
    """Буфер обмена для совета: `found` — что вернёт проверка (метка, совет) или None."""

    def __init__(self, monkeypatch, found=None) -> None:
        self.found = found
        self.checks = 0
        monkeypatch.setattr(image_hints, "read_clipboard_tip", self._read)

    def _read(self):
        self.checks += 1
        return self.found


def _footer(pilot) -> str:
    return str(pilot.app.screen.query_one("#footer-left", Static).render())


def _input(pilot) -> CommandTextArea:
    return pilot.app.screen.query_one("#cmdinput", CommandTextArea)


async def _settle(pilot) -> None:
    """Дождаться проверки буфера и перерисовки."""
    await pilot.pause()
    for worker in list(pilot.app.workers):
        if worker.group == "clipboard-tip" and not worker.is_finished:
            await worker.wait()
    await pilot.pause()


async def _return_to_terminal(pilot) -> None:
    pilot.app.post_message(events.AppBlur())
    await pilot.pause()
    pilot.app.post_message(events.AppFocus())
    await _settle(pilot)


async def _paste_image(pilot, monkeypatch, color=(200, 30, 30)) -> None:
    monkeypatch.setattr(os_ops, "read_clipboard", lambda: "")
    monkeypatch.setattr(os_ops, "read_clipboard_image", lambda: _png(color))
    await pilot.press("alt+v")
    await pilot.pause()
    await pilot.app.screen._image_paste_worker.wait()
    await pilot.pause()


class _Modal(ModalScreen):
    def compose(self):
        yield Static("диалог")


# --- совет вставить из буфера ----------------------------------------------------


@pytest.mark.asyncio
async def test_an_image_in_the_clipboard_at_start_shows_the_tip(
    tmp_path, monkeypatch
):
    _Clipboard(monkeypatch, ("1", TIP))
    async with _app(tmp_path).run_test() as pilot:
        await _settle(pilot)
        assert _footer(pilot) == TIP


@pytest.mark.asyncio
async def test_returning_to_the_terminal_shows_the_tip_once_per_clipboard(
    tmp_path, monkeypatch
):
    clipboard = _Clipboard(monkeypatch)
    async with _app(tmp_path).run_test() as pilot:
        await _settle(pilot)
        commands = _footer(pilot)

        clipboard.found = ("1", TIP)  # скопировали картинку снаружи
        await _return_to_terminal(pilot)
        assert _footer(pilot) == TIP

        pilot.app.screen._clear_footer_hint()  # совет погас
        await _return_to_terminal(pilot)  # в буфере то же самое
        assert _footer(pilot) == commands

        clipboard.found = ("2", "в буфере 3 изображения · Cmd+V — вставить")
        await _return_to_terminal(pilot)  # буфер изменился
        assert _footer(pilot) == "в буфере 3 изображения · Cmd+V — вставить"


@pytest.mark.asyncio
async def test_focus_moves_inside_the_app_do_not_check_the_clipboard(
    tmp_path, monkeypatch
):
    clipboard = _Clipboard(monkeypatch)
    async with _app(tmp_path).run_test() as pilot:
        await _settle(pilot)
        checks = clipboard.checks
        await pilot.app.push_screen(_Modal())
        await pilot.pause()
        pilot.app.pop_screen()
        await pilot.press("tab", "shift+tab")
        await _settle(pilot)
        assert clipboard.checks == checks


@pytest.mark.asyncio
async def test_the_next_action_puts_the_tip_out(tmp_path, monkeypatch):
    _Clipboard(monkeypatch, ("1", TIP))
    async with _app(tmp_path).run_test() as pilot:
        await _settle(pilot)
        assert _footer(pilot) == TIP
        await pilot.press("x")
        await pilot.pause()
        assert _footer(pilot) != TIP


@pytest.mark.asyncio
async def test_a_busy_footer_keeps_its_hint_and_the_tip_waits(
    tmp_path, monkeypatch
):
    clipboard = _Clipboard(monkeypatch)
    async with _app(tmp_path).run_test() as pilot:
        await _settle(pilot)
        scr = pilot.app.screen
        scr._show_footer_hint("чужая подсказка", seconds=60)
        clipboard.found = ("1", TIP)
        await _return_to_terminal(pilot)
        assert _footer(pilot) == "чужая подсказка"

        scr._clear_footer_hint()
        await _return_to_terminal(pilot)  # тот же буфер: совет ещё не звучал
        assert _footer(pilot) == TIP


@pytest.mark.asyncio
async def test_no_tip_over_a_dialog_or_a_disabled_input(tmp_path, monkeypatch):
    clipboard = _Clipboard(monkeypatch)
    async with _app(tmp_path).run_test() as pilot:
        await _settle(pilot)
        clipboard.found = ("1", TIP)
        await pilot.app.push_screen(_Modal())
        await _return_to_terminal(pilot)
        pilot.app.pop_screen()
        await pilot.pause()
        assert _footer(pilot) != TIP

        _input(pilot).disabled = True  # баннер подтверждения держит ввод
        await _return_to_terminal(pilot)
        assert _footer(pilot) != TIP

        _input(pilot).disabled = False
        await _return_to_terminal(pilot)
        assert _footer(pilot) == TIP


@pytest.mark.asyncio
async def test_a_pasted_clipboard_gets_no_tip_later(tmp_path, monkeypatch):
    """Буфер проверили, пока футер был занят, а картинку из него вставили: подсказывать
    о ней при следующем возвращении в окно поздно."""
    clipboard = _Clipboard(monkeypatch)
    async with _app(tmp_path).run_test() as pilot:
        await _settle(pilot)
        scr = pilot.app.screen
        scr._show_footer_hint("чужая подсказка", seconds=60)
        clipboard.found = ("1", TIP)
        await _return_to_terminal(pilot)
        scr._clear_footer_hint()

        await _paste_image(pilot, monkeypatch)
        scr._clear_footer_hint()  # совет про Ctrl+клик погас
        await _return_to_terminal(pilot)
        assert _footer(pilot) != TIP


# --- совет про Ctrl+клик ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_inserted_image_tells_how_to_open_it_once(
    tmp_path, monkeypatch
):
    _Clipboard(monkeypatch)
    async with _app(tmp_path).run_test() as pilot:
        await _settle(pilot)
        await _paste_image(pilot, monkeypatch)
        assert _input(pilot).text == "[Изображение #1]"
        assert _footer(pilot) == image_hints.IMAGE_OPEN_TIP

        pilot.app.screen._clear_footer_hint()
        await _paste_image(pilot, monkeypatch, color=(1, 2, 3))
        assert "[Изображение #2]" in _input(pilot).text
        assert _footer(pilot) != image_hints.IMAGE_OPEN_TIP


def _gate_file_check(monkeypatch) -> threading.Event:
    """Проверка файла ждёт сигнала: следующий путь приходит, пока читается первый."""
    from gc_tui.services import image_paste

    gate = threading.Event()
    real = image_paste.is_image_file

    def gated(path):
        gate.wait(5)
        return real(path)

    monkeypatch.setattr(image_paste, "is_image_file", gated)
    return gate


def _files(tmp_path, count: int) -> list[Path]:
    paths = []
    for i in range(count):
        path = Path(tmp_path) / f"shot{i}.png"
        path.write_bytes(_png((i, 2, 3)))
        paths.append(path)
    return paths


async def _paste_queued(pilot, monkeypatch, paths) -> None:
    """Cmd+V нескольких файлов из Finder: каждый путь — отдельной вставкой, следующие
    ждут в очереди, пока читается первый."""
    gate = _gate_file_check(monkeypatch)
    inp = _input(pilot)
    try:
        for path in paths:
            inp._handle_paste(f"{path} ")
        await pilot.pause()
    finally:
        gate.set()
    await pilot.pause()
    await pilot.app.screen._image_paste_worker.wait()
    await pilot.pause()


@pytest.mark.asyncio
async def test_the_open_tip_comes_after_the_whole_file_queue(
    tmp_path, monkeypatch
):
    """Совет звучит после последней плашки очереди — следующая плашка его не гасит."""
    _Clipboard(monkeypatch)
    async with _app(tmp_path).run_test() as pilot:
        await _settle(pilot)
        await _paste_queued(pilot, monkeypatch, _files(tmp_path, 2))
        assert _input(pilot).text == "[Изображение #1] [Изображение #2]"
        assert _footer(pilot) == image_hints.IMAGE_OPEN_TIP


@pytest.mark.asyncio
async def test_the_open_tip_yields_to_a_refusal_and_waits(
    tmp_path, monkeypatch
):
    """Лишний файл очереди — отказ лимита: он важнее совета, а совет ждёт следующей
    вставленной картинки."""
    _Clipboard(monkeypatch)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {
                "agent.model": "M",
                "paths.threads_data_path": str(tmp_path),
                "images": {"max_per_message": 1},
            }
        ),
    )
    async with app.run_test() as pilot:
        await _settle(pilot)
        await _paste_queued(pilot, monkeypatch, _files(tmp_path, 2))
        assert "✗ не больше 1 изображений в одном сообщении" in _footer(pilot)

        pilot.app.screen._clear_footer_hint()
        _input(pilot).load_text("")
        await _paste_image(pilot, monkeypatch, color=(9, 9, 9))
        assert _footer(pilot) == image_hints.IMAGE_OPEN_TIP
