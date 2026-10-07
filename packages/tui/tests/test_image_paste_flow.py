"""Вставка изображения в поле ввода (спека §12, визуальный контракт §2): Alt+V / alt+м,
порядок Ctrl+V и правого клика, пустая вставка, путь к файлу, лимит и отказы, нумерация,
чтение вне UI-потока. Слой ОС подменён: настоящий буфер обмена тесты не читают."""

import io
import json
import sys
import threading
from pathlib import Path

import pytest
from PIL import Image
from textual import events
from textual.widgets import Static
from textual.widgets.text_area import Selection

from gc_tui.app import TuiApp
from gc_tui.services import os_ops
from gc_tui.widgets.command_input import CommandTextArea

SHOT = "Снимок экрана 2026-09-24 в\u202f10.00.00.png"


def _png(w: int = 64, h: int = 48, color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _app(tmp_path, **config) -> TuiApp:
    # Каталог тредов и `threads_data_path` — одно значение, как на старте TUI: корень
    # хранилища картинок приложение считает от конфига (`ImageStore.for_config`).
    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        config={"model": "M", "threads_data_path": str(tmp_path), **config},
    )


def _clipboard(monkeypatch, *, text: str = "", image=None) -> None:
    """Подменить слой ОС: тесты не читают настоящий буфер обмена разработчика."""
    monkeypatch.setattr(os_ops, "read_clipboard", lambda: text)
    monkeypatch.setattr(os_ops, "read_clipboard_image", lambda: image)


def _input(pilot) -> CommandTextArea:
    return pilot.app.screen.query_one("#cmdinput", CommandTextArea)


def _footer(pilot) -> str:
    return str(pilot.app.screen.query_one("#footer-left", Static).render())


async def _settle(pilot) -> None:
    """Доставить сообщение вставки, дождаться воркера и перерисовки."""
    await pilot.pause()
    await pilot.app.screen._image_paste_worker.wait()
    await pilot.pause()


def _write_thread(tmp_path, tid: str, *, image_seq: int) -> None:
    meta = {
        "thread_id": tid,
        "system_id": "cli",
        "runtime": {"image_seq": image_seq},
    }
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    rec = {
        "type": "message",
        "thread_id": tid,
        "system": "cli",
        "seq": 1,
        "ts": "t",
        "payload": {"role": "user", "content": "раньше"},
    }
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(
        json.dumps(rec) + "\n", encoding="utf-8"
    )


# --- источники ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_alt_v_inserts_the_clipboard_image_as_a_chip(
    tmp_path, monkeypatch
):
    from gc_core.attachments.model import ORIGIN_USER
    from gc_core.attachments.store import ImageStore

    _clipboard(monkeypatch, image=_png(64, 48))
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("alt+v")
        await _settle(pilot)
        inp = _input(pilot)
        assert inp.text == "[Изображение #1]"
        (att,) = inp.images_in_buffer()
        assert (att.label, att.origin, att.source) == (
            "Изображение #1",
            ORIGIN_USER,
            "clipboard",
        )
        assert (att.width, att.height) == (64, 48)
        staging = Path(
            ImageStore.for_threads_dir(str(tmp_path)).staging_dir
        ).resolve()
        assert Path(att.path).resolve().parent == staging
        assert "Вставка…" not in _footer(pilot)


@pytest.mark.asyncio
async def test_alt_v_works_on_the_russian_layout(tmp_path, monkeypatch):
    _clipboard(monkeypatch, image=_png())
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("alt+м")
        await _settle(pilot)
        assert _input(pilot).text == "[Изображение #1]"


@pytest.mark.asyncio
async def test_text_with_an_image_ctrl_v_takes_the_text_alt_v_takes_the_image(
    tmp_path, monkeypatch
):
    """В буфере и текст, и картинка (Review Focus #2): Ctrl+V вставляет текст, Alt+V —
    картинку."""
    reads = []

    def image():
        reads.append(1)
        return _png()

    monkeypatch.setattr(
        os_ops, "read_clipboard", lambda: "https://example.com/cat.png"
    )
    monkeypatch.setattr(os_ops, "read_clipboard_image", image)
    async with _app(tmp_path).run_test() as pilot:
        inp = _input(pilot)
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert inp.text == "https://example.com/cat.png"
        assert reads == []  # текст есть — картинку Ctrl+V не читает
        await pilot.press("alt+v")
        await _settle(pilot)
        assert inp.text == "https://example.com/cat.png [Изображение #1]"
        assert reads == [1]


def _finder_copy(monkeypatch, *files: Path) -> None:
    """Копия в Finder: в буфере файлы и рядом их имена текстом (через `\\r`)."""
    monkeypatch.setattr(
        os_ops, "read_clipboard", lambda: "\r".join(f.name for f in files)
    )
    monkeypatch.setattr(
        os_ops, "read_clipboard_files", lambda: tuple(str(f) for f in files)
    )
    monkeypatch.setattr(os_ops, "read_clipboard_image", lambda: None)
    monkeypatch.setattr(os_ops, "copied_files_come_with_names", lambda: True)


@pytest.mark.asyncio
async def test_ctrl_v_takes_an_image_file_copied_in_finder(
    tmp_path, monkeypatch
):
    """Имя, которое Finder кладёт рядом текстом, не мешает: Ctrl+V берёт сам файл."""
    shot = tmp_path / SHOT
    shot.write_bytes(_png())
    _finder_copy(monkeypatch, shot)
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("ctrl+v")
        await _settle(pilot)
        inp = _input(pilot)
        assert inp.text == "[Изображение #1]"
        assert [a.source for a in inp.images_in_buffer()] == [str(shot)]


@pytest.mark.asyncio
async def test_ctrl_v_takes_several_copied_image_files(tmp_path, monkeypatch):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(_png(color=(1, 2, 3)))
    b.write_bytes(_png(color=(4, 5, 6)))
    _finder_copy(monkeypatch, a, b)
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("ctrl+v")
        await _settle(pilot)
        assert _input(pilot).text == "[Изображение #1] [Изображение #2]"


@pytest.mark.asyncio
async def test_ctrl_v_pastes_names_when_a_copied_file_is_not_an_image(
    tmp_path, monkeypatch
):
    a = tmp_path / "a.png"
    a.write_bytes(_png())
    notes = tmp_path / "notes.txt"
    notes.write_text("текст", encoding="utf-8")
    _finder_copy(monkeypatch, a, notes)
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("ctrl+v")
        await _settle(pilot)
        assert _input(pilot).text == "a.png\nnotes.txt"
        assert "✗" not in _footer(pilot)


@pytest.mark.asyncio
async def test_ctrl_v_text_goes_in_at_once_where_files_bring_no_names(
    tmp_path, monkeypatch
):
    """Windows и Linux не кладут имена скопированных файлов текстом: искать файлы
    незачем, текст встаёт сразу — без фоновой вставки (ревью 2026-10-01)."""
    _clipboard(monkeypatch, text="просто текст")
    monkeypatch.setattr(os_ops, "copied_files_come_with_names", lambda: False)
    monkeypatch.setattr(
        os_ops, "read_clipboard_files", lambda: pytest.fail("файлы не ищет")
    )
    async with _app(tmp_path).run_test() as pilot:
        inp = _input(pilot)
        inp.action_paste()
        assert inp.text == "просто текст"


@pytest.mark.asyncio
async def test_second_ctrl_v_during_the_files_check_waits_its_turn(
    tmp_path, monkeypatch
):
    """Второе нажатие, пока первое ищет файлы, не теряется и не обгоняет первое:
    ждёт в очереди и проходит ту же проверку файлов — имена файлов Finder текстом
    не проскочат (ревью 2026-10-01)."""
    gate = threading.Event()

    def slow_files():
        gate.wait(5)
        return ()

    _clipboard(monkeypatch, text="текст")
    monkeypatch.setattr(os_ops, "copied_files_come_with_names", lambda: True)
    monkeypatch.setattr(os_ops, "read_clipboard_files", slow_files)
    async with _app(tmp_path).run_test() as pilot:
        inp = _input(pilot)
        await pilot.press("ctrl+v")
        await pilot.pause()
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert inp.text == ""
        gate.set()
        await _settle(pilot)
        assert inp.text == "тексттекст"


@pytest.mark.asyncio
async def test_ctrl_v_without_text_takes_the_image(tmp_path, monkeypatch):
    _clipboard(monkeypatch, image=_png())
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("ctrl+v")
        await _settle(pilot)
        assert _input(pilot).text == "[Изображение #1]"


@pytest.mark.asyncio
async def test_ctrl_v_without_text_or_image_pastes_the_app_clipboard(
    tmp_path, monkeypatch
):
    _clipboard(monkeypatch)
    async with _app(tmp_path).run_test() as pilot:
        pilot.app.copy_to_clipboard("из приложения")
        await pilot.press("ctrl+v")
        await _settle(pilot)
        assert _input(pilot).text == "из приложения"
        assert "✗" not in _footer(
            pilot
        )  # не отказ: Ctrl+V молча берёт свой буфер


@pytest.mark.asyncio
async def test_right_click_follows_the_ctrl_v_order(tmp_path, monkeypatch):
    _clipboard(monkeypatch, image=_png())
    async with _app(tmp_path).run_test() as pilot:
        inp = _input(pilot)
        await inp._on_mouse_down(
            events.MouseDown(
                widget=inp,
                x=0,
                y=0,
                delta_x=0,
                delta_y=0,
                button=3,
                shift=False,
                meta=False,
                ctrl=False,
                screen_x=0,
                screen_y=0,
            )
        )
        await _settle(pilot)
        assert inp.text == "[Изображение #1]"


@pytest.mark.asyncio
async def test_alt_v_without_an_image_pastes_the_text(tmp_path, monkeypatch):
    _clipboard(monkeypatch, text="просто текст")
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("alt+v")
        await _settle(pilot)
        assert _input(pilot).text == "просто текст"


@pytest.mark.asyncio
async def test_alt_v_on_an_empty_clipboard_refuses(tmp_path, monkeypatch):
    _clipboard(monkeypatch)
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("alt+v")
        await _settle(pilot)
        assert _input(pilot).text == ""
        assert "✗ в буфере обмена нет изображения" in _footer(pilot)


@pytest.mark.asyncio
async def test_empty_paste_takes_the_clipboard_image(tmp_path, monkeypatch):
    _clipboard(monkeypatch, image=_png())
    async with _app(tmp_path).run_test() as pilot:
        inp = _input(pilot)
        inp.post_message(events.Paste(""))
        await _settle(pilot)
        assert inp.text == "[Изображение #1]"


@pytest.mark.asyncio
async def test_empty_paste_without_an_image_refuses_and_keeps_the_selection(
    tmp_path, monkeypatch
):
    """Раньше пустая вставка заменяла выделение пустой строкой — стирала его."""
    _clipboard(monkeypatch)
    async with _app(tmp_path).run_test() as pilot:
        inp = _input(pilot)
        inp.text = "оставить это"
        inp.selection = Selection((0, 0), (0, 8))
        inp.post_message(events.Paste(""))
        await _settle(pilot)
        assert inp.text == "оставить это"
        assert inp.selection == Selection((0, 0), (0, 8))
        assert "✗ в буфере обмена нет изображения" in _footer(pilot)


@pytest.mark.asyncio
async def test_empty_paste_with_focus_in_the_trace_goes_to_the_input(
    tmp_path, monkeypatch
):
    from gc_tui.widgets.trace_row import TraceRow

    _clipboard(monkeypatch, image=_png())
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        row = TraceRow(
            prefix=">", content="строка", kind_class="line-agent", event_no=1
        )
        scr.query_one("#trace").mount(row)
        await pilot.pause()
        row.focus()
        await pilot.pause()
        scr.post_message(
            events.Paste("")
        )  # экранный фолбэк main.py `_on_paste`
        await _settle(pilot)
        assert _input(pilot).text == "[Изображение #1]"


@pytest.mark.parametrize(
    "form",
    [
        "raw",
        "quoted",
        "file_url",
        pytest.param(
            "escaped",
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason="\\-экранирование — только вне Windows",
            ),
        ),
    ],
)
@pytest.mark.asyncio
async def test_pasted_screenshot_path_becomes_an_image_chip(
    tmp_path, monkeypatch, form
):
    """Путь из Finder: пробелы, кириллица, U+202F; как есть, в кавычках, file:// и с `\\ `
    (Review Focus #3)."""
    shot = tmp_path / "Desktop" / SHOT
    shot.parent.mkdir()
    shot.write_bytes(_png())
    text = {
        "raw": str(shot),
        "quoted": f"'{shot}'",
        "file_url": shot.as_uri(),
        "escaped": str(shot).replace(" ", "\\ ")
        + " ",  # Terminal.app: `\ ` и пробел в конце
    }[form]
    _clipboard(monkeypatch)
    async with _app(tmp_path).run_test() as pilot:
        inp = _input(pilot)
        inp._handle_paste(text)
        await _settle(pilot)
        assert inp.text == "[Изображение #1]"
        assert [a.source for a in inp.images_in_buffer()] == [str(shot)]


@pytest.mark.asyncio
async def test_pasted_path_to_a_non_image_stays_text(tmp_path, monkeypatch):
    notes = tmp_path / "notes.txt"
    notes.write_text("не картинка", encoding="utf-8")
    _clipboard(monkeypatch)
    async with _app(tmp_path).run_test() as pilot:
        inp = _input(pilot)
        inp._handle_paste(f"'{notes}'")
        await _settle(pilot)
        assert (
            inp.text == f"'{notes}'"
        )  # вставлен исходный текст, кавычки на месте
        assert inp.images_in_buffer() == ()


# --- лимит и отказы -----------------------------------------------------------


@pytest.mark.asyncio
async def test_alt_v_at_the_limit_refuses_the_found_image(
    tmp_path, monkeypatch
):
    colors = iter([(1, 1, 1), (2, 2, 2), (3, 3, 3)])
    _clipboard(monkeypatch)
    monkeypatch.setattr(
        os_ops, "read_clipboard_image", lambda: _png(color=next(colors))
    )
    async with _app(
        tmp_path, images={"max_per_message": 2}
    ).run_test() as pilot:
        for _ in range(3):
            await pilot.press("alt+v")
            await _settle(pilot)
        assert _input(pilot).text == "[Изображение #1] [Изображение #2]"
        assert "✗ не больше 2 изображений в одном сообщении" in _footer(pilot)


@pytest.mark.asyncio
async def test_alt_v_at_the_limit_with_text_in_the_clipboard_pastes_the_text(
    tmp_path, monkeypatch
):
    """Лимит — только когда изображение найдено: текст в буфере вставляется и на полном."""
    _clipboard(monkeypatch, image=_png())
    async with _app(
        tmp_path, images={"max_per_message": 1}
    ).run_test() as pilot:
        await pilot.press("alt+v")
        await _settle(pilot)
        _clipboard(monkeypatch, text="и текст")
        await pilot.press("alt+v")
        await _settle(pilot)
        assert _input(pilot).text == "[Изображение #1] и текст"
        assert "✗" not in _footer(pilot)


@pytest.mark.asyncio
async def test_ctrl_v_at_the_limit_pastes_the_app_clipboard(
    tmp_path, monkeypatch
):
    """Ctrl+V и правый клик картинку не заказывали: полный лимит не блокирует вставку —
    вставляется внутренний буфер приложения, а отказ в футере объясняет, почему не
    плашка."""
    _clipboard(monkeypatch, image=_png())
    async with _app(
        tmp_path, images={"max_per_message": 1}
    ).run_test() as pilot:
        await pilot.press("alt+v")
        await _settle(pilot)
        pilot.app.copy_to_clipboard("из приложения")
        await pilot.press("ctrl+v")
        await _settle(pilot)
        assert _input(pilot).text == "[Изображение #1] из приложения"
        assert "✗ не больше 1 изображений в одном сообщении" in _footer(pilot)


@pytest.mark.asyncio
async def test_copied_files_over_the_limit_are_refused_whole(
    tmp_path, monkeypatch
):
    files = []
    for name, color in (("a", (1, 1, 1)), ("b", (2, 2, 2)), ("c", (3, 3, 3))):
        f = tmp_path / f"{name}.png"
        f.write_bytes(_png(color=color))
        files.append(str(f))
    _clipboard(monkeypatch, image=files)
    async with _app(
        tmp_path, images={"max_per_message": 2}
    ).run_test() as pilot:
        await pilot.press("alt+v")
        await _settle(pilot)
        assert _input(pilot).text == ""
        assert "✗ не больше 2 изображений в одном сообщении" in _footer(pilot)


@pytest.mark.asyncio
async def test_bytes_that_are_not_an_image_are_refused(tmp_path, monkeypatch):
    _clipboard(monkeypatch, image=b"definitely not an image")
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("alt+v")
        await _settle(pilot)
        assert _input(pilot).text == ""
        assert "✗ это не изображение" in _footer(pilot)


@pytest.mark.asyncio
async def test_an_oversized_image_is_refused(tmp_path, monkeypatch):
    from gc_core.attachments.model import MAX_INPUT_BYTES

    _clipboard(monkeypatch, image=b"\0" * (MAX_INPUT_BYTES + 1))
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("alt+v")
        await _settle(pilot)
        assert _input(pilot).text == ""
        assert "✗ изображение слишком большое" in _footer(pilot)


def _no_clipboard_tool():
    raise os_ops.ClipboardToolMissing("нет ни wl-paste, ни xclip")


@pytest.mark.asyncio
async def test_a_missing_clipboard_tool_is_refused(tmp_path, monkeypatch):
    _clipboard(monkeypatch)
    monkeypatch.setattr(os_ops, "read_clipboard_image", _no_clipboard_tool)
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("alt+v")
        await _settle(pilot)
        assert "✗ для вставки изображений нужен wl-paste или xclip" in _footer(
            pilot
        )


@pytest.mark.asyncio
async def test_ctrl_v_without_a_clipboard_tool_pastes_the_app_clipboard(
    tmp_path, monkeypatch
):
    """Регрессия: Linux без wl-paste и xclip — Ctrl+V вставлял отказ вместо своего буфера."""
    _clipboard(monkeypatch)
    monkeypatch.setattr(os_ops, "read_clipboard_image", _no_clipboard_tool)
    async with _app(tmp_path).run_test() as pilot:
        pilot.app.copy_to_clipboard("из приложения")
        await pilot.press("ctrl+v")
        await _settle(pilot)
        assert _input(pilot).text == "из приложения"
        assert "✗" not in _footer(pilot)


@pytest.mark.asyncio
async def test_an_unwritable_pocket_is_refused_as_a_save_failure(
    tmp_path, monkeypatch
):
    (tmp_path / "images").write_text(
        "файл на месте каталога картинок", encoding="utf-8"
    )
    _clipboard(monkeypatch, image=_png())
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("alt+v")
        await _settle(pilot)
        assert _input(pilot).text == ""
        assert "✗ не удалось сохранить изображение" in _footer(pilot)


@pytest.mark.asyncio
async def test_copied_image_files_go_in_as_one_edit(tmp_path, monkeypatch):
    a = tmp_path / "a.png"
    a.write_bytes(_png(color=(1, 2, 3)))
    b = tmp_path / "b.png"
    b.write_bytes(_png(color=(4, 5, 6)))
    _clipboard(monkeypatch, image=[str(a), str(b)])
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("alt+v")
        await _settle(pilot)
        inp = _input(pilot)
        assert inp.text == "[Изображение #1] [Изображение #2]"
        assert [x.source for x in inp.images_in_buffer()] == [str(a), str(b)]
        inp.action_undo()
        await pilot.pause()
        assert inp.text == ""  # одна правка — один шаг Ctrl+Z


@pytest.mark.asyncio
async def test_copied_files_with_a_non_image_are_refused_whole(
    tmp_path, monkeypatch
):
    a = tmp_path / "a.png"
    a.write_bytes(_png())
    notes = tmp_path / "notes.txt"
    notes.write_text("не картинка", encoding="utf-8")
    _clipboard(monkeypatch, image=[str(a), str(notes)])
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("alt+v")
        await _settle(pilot)
        assert _input(pilot).text == ""
        assert "✗ это не изображение" in _footer(pilot)


# --- вне UI-потока (Review Focus #1) -------------------------------------------


@pytest.mark.asyncio
async def test_reading_runs_off_the_ui_thread_and_enter_waits(
    tmp_path, monkeypatch
):
    """Чтение буфера не морозит TUI: печать доходит, Enter до конца чтения не отправляет."""
    release = threading.Event()
    on_ui_thread = []

    def slow_image():
        on_ui_thread.append(
            threading.current_thread() is threading.main_thread()
        )
        release.wait(5)
        return _png()

    _clipboard(monkeypatch)
    monkeypatch.setattr(os_ops, "read_clipboard_image", slow_image)
    async with _app(tmp_path).run_test() as pilot:
        inp = _input(pilot)
        try:
            await pilot.press("alt+v")
            assert "Вставка…" in _footer(pilot)
            await pilot.press("x")  # цикл UI свободен: печать доходит
            assert inp.text == "x"
            await pilot.press("enter")  # чтение не кончилось — не отправка
            assert inp.text == "x"
            assert "дождитесь окончания вставки" in _footer(pilot)
        finally:
            release.set()
        await _settle(pilot)
        assert inp.text == "x [Изображение #1]"
        assert "Вставка…" not in _footer(pilot)
    assert on_ui_thread == [False]


@pytest.mark.asyncio
async def test_a_pasted_path_is_checked_and_normalized_off_the_ui_thread(
    tmp_path, monkeypatch
):
    """Review Focus №1, половина TUI: проверка байтов файла и Pillow — не на UI-потоке."""
    from gc_core.attachments import store as store_module
    from gc_tui.services import image_paste

    seen: dict = {"is_image_file": [], "normalize_image": []}
    real_check, real_normalize = (
        image_paste.is_image_file,
        store_module.normalize_image,
    )

    def check(path):
        seen["is_image_file"].append(
            threading.current_thread() is threading.main_thread()
        )
        return real_check(path)

    def normalize(data):
        seen["normalize_image"].append(
            threading.current_thread() is threading.main_thread()
        )
        return real_normalize(data)

    monkeypatch.setattr(image_paste, "is_image_file", check)
    monkeypatch.setattr(store_module, "normalize_image", normalize)
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png())
    _clipboard(monkeypatch)
    async with _app(tmp_path).run_test() as pilot:
        inp = _input(pilot)
        inp._handle_paste(str(shot))
        await _settle(pilot)
        assert inp.text == "[Изображение #1]"
    assert seen == {"is_image_file": [False], "normalize_image": [False]}


@pytest.mark.asyncio
async def test_alt_v_reads_the_os_text_off_the_ui_thread(
    tmp_path, monkeypatch
):
    """Alt+V без картинки читает текст буфера ОС — на Linux до трёх процессов по 2 с."""
    on_ui_thread = []

    def text():
        on_ui_thread.append(
            threading.current_thread() is threading.main_thread()
        )
        return "текст"

    _clipboard(monkeypatch)
    monkeypatch.setattr(os_ops, "read_clipboard", text)
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("alt+v")
        await _settle(pilot)
        assert _input(pilot).text == "текст"
    assert on_ui_thread == [False]


# --- долгое чтение: подсказка, потолок, отмена -------------------------------------


def _slow_clipboard(
    monkeypatch, release: threading.Event, result=None
) -> None:
    def slow():
        release.wait(5)
        return result

    _clipboard(monkeypatch)
    monkeypatch.setattr(os_ops, "read_clipboard_image", slow)


@pytest.mark.asyncio
async def test_busy_hint_comes_back_after_the_wait_hint(tmp_path, monkeypatch):
    """Enter во время чтения мигает «дождитесь…», но покой футера — «Вставка…», пока
    чтение идёт (подсказка «дождитесь» гаснет своим таймером)."""
    release = threading.Event()
    _slow_clipboard(monkeypatch, release, _png())
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        try:
            await pilot.press("alt+v")
            await pilot.press("enter")
            assert "дождитесь окончания вставки" in _footer(pilot)
            scr._clear_footer_hint()  # таймер «дождитесь…» истёк
            assert _footer(pilot) == "Вставка…"
        finally:
            release.set()
        await _settle(pilot)
        assert "Вставка…" not in _footer(pilot)


@pytest.mark.asyncio
async def test_end_of_the_paste_keeps_someone_elses_hint(
    tmp_path, monkeypatch
):
    """Конец чтения снимает только свою подсказку: «вставьте ещё раз…» от крупной
    вставки текста во время чтения остаётся."""
    release = threading.Event()
    _slow_clipboard(monkeypatch, release)
    async with _app(tmp_path).run_test() as pilot:
        inp = _input(pilot)
        try:
            await pilot.press("ctrl+v")  # текста нет — читается картинка
            inp._handle_paste("строка\n" * 6)
            await pilot.pause()
            assert "вставьте ещё раз, чтобы развернуть" in _footer(pilot)
        finally:
            release.set()
        await _settle(pilot)
        assert "вставьте ещё раз, чтобы развернуть" in _footer(pilot)


@pytest.mark.asyncio
async def test_a_hung_read_is_refused_after_the_ceiling(tmp_path, monkeypatch):
    from gc_tui.services import image_paste

    release = threading.Event()
    _slow_clipboard(monkeypatch, release, _png())
    monkeypatch.setattr(image_paste, "PASTE_TIMEOUT_SECONDS", 0.1)
    async with _app(tmp_path).run_test() as pilot:
        try:
            await pilot.press("alt+v")
            await _settle(pilot)
            assert pilot.app.screen._image_paste_busy is False
            assert "✗ вставка не удалась: истекло время ожидания" in _footer(
                pilot
            )
            assert _input(pilot).text == ""
        finally:
            release.set()


@pytest.mark.asyncio
async def test_double_esc_cancels_the_paste(tmp_path, monkeypatch):
    release = threading.Event()
    _slow_clipboard(monkeypatch, release, _png())
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        try:
            await pilot.press("alt+v")
            await pilot.press("escape")
            assert "Esc ещё раз, чтобы отменить вставку" in _footer(pilot)
            await pilot.press("escape")
            await pilot.pause()
            assert scr._image_paste_busy is False
            assert "Вставка…" not in _footer(pilot)
        finally:
            release.set()
        await pilot.pause(0.2)
        assert _input(pilot).text == ""  # отменённое чтение плашку не вставило


@pytest.mark.asyncio
async def test_a_paste_failure_does_not_kill_the_app(tmp_path, monkeypatch):
    from gc_tui.services import image_paste

    async def boom(*_a, **_k):
        raise RuntimeError("сбой вставки")

    _clipboard(monkeypatch)
    monkeypatch.setattr(image_paste, "astage_paste", boom)
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("alt+v")
        await _settle(pilot)
        assert pilot.app.is_running
        assert pilot.app.screen._image_paste_busy is False
        assert "Вставка…" not in _footer(pilot)


@pytest.mark.asyncio
async def test_a_retina_5k_screenshot_is_scaled_down(tmp_path, monkeypatch):
    _clipboard(monkeypatch, image=_png(5120, 2880))
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("alt+v")
        await _settle(pilot)
        (att,) = _input(pilot).images_in_buffer()
        assert (att.width, att.height) == (1568, 882)
        assert (att.orig_width, att.orig_height) == (5120, 2880)


def _gate_file_check(monkeypatch) -> threading.Event:
    """Проверка файла ждёт сигнала: следующая вставка приходит, пока идёт чтение."""
    from gc_tui.services import image_paste

    gate = threading.Event()
    real = image_paste.is_image_file

    def gated(path):
        gate.wait(5)
        return real(path)

    monkeypatch.setattr(image_paste, "is_image_file", gated)
    return gate


def _terminal_paste(path: Path) -> str:
    """Путь так, как его присылает Terminal.app (Cmd+V из Finder, перетаскивание):
    пробелы экранированы, в конце пробел; каждый файл — отдельной вставкой."""
    return str(path).replace(" ", "\\ ") + " "


@pytest.mark.asyncio
async def test_a_path_pasted_during_a_read_waits_its_turn(
    tmp_path, monkeypatch
):
    release = threading.Event()
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png())

    def slow_empty():
        release.wait(5)
        return None

    _clipboard(monkeypatch)
    monkeypatch.setattr(os_ops, "read_clipboard_image", slow_empty)
    async with _app(tmp_path).run_test() as pilot:
        inp = _input(pilot)
        try:
            await pilot.press("alt+v")
            inp._handle_paste(str(shot))
            await pilot.pause()
            assert inp.text == ""  # ждёт в очереди, текстом не ушёл
        finally:
            release.set()
        await _settle(pilot)
        assert inp.text == "[Изображение #1]"


@pytest.mark.asyncio
async def test_files_pasted_one_by_one_become_chips_in_order(
    tmp_path, monkeypatch
):
    """Cmd+V после копирования в Finder и перетаскивание нескольких файлов: терминал
    присылает каждый путь отдельной вставкой, второй — пока читается первый."""
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    first = desktop / SHOT
    first.write_bytes(_png(color=(1, 2, 3)))
    second = desktop / "Снимок экрана 2026-09-25 в\u202f00.47.49.png"
    second.write_bytes(_png(color=(4, 5, 6)))
    gate = _gate_file_check(monkeypatch)
    _clipboard(monkeypatch)
    async with _app(tmp_path).run_test() as pilot:
        inp = _input(pilot)
        try:
            inp._handle_paste(_terminal_paste(first))
            inp._handle_paste(_terminal_paste(second))
            await pilot.pause()
            assert inp.text == ""
        finally:
            gate.set()
        await _settle(pilot)
        assert inp.text == "[Изображение #1] [Изображение #2]"
        assert [a.source for a in inp.images_in_buffer()] == [
            str(first),
            str(second),
        ]


@pytest.mark.asyncio
async def test_a_file_over_the_limit_goes_in_as_its_path_with_the_limit_refusal(
    tmp_path, monkeypatch
):
    """Cmd+V одиннадцати файлов из Finder: лишний путь вставляется текстом (не теряется),
    а отказ в футере объясняет, почему это не плашка."""
    first = tmp_path / "a.png"
    first.write_bytes(_png(color=(1, 2, 3)))
    extra = tmp_path / "b.png"
    extra.write_bytes(_png(color=(4, 5, 6)))
    gate = _gate_file_check(monkeypatch)
    _clipboard(monkeypatch)
    async with _app(
        tmp_path, images={"max_per_message": 1}
    ).run_test() as pilot:
        inp = _input(pilot)
        try:
            inp._handle_paste(_terminal_paste(first))
            inp._handle_paste(_terminal_paste(extra))
            await pilot.pause()
        finally:
            gate.set()
        await _settle(pilot)
        assert inp.text == f"[Изображение #1] {_terminal_paste(extra)}"
        assert "✗ не больше 1 изображений в одном сообщении" in _footer(pilot)


@pytest.mark.asyncio
async def test_a_queued_non_image_goes_in_as_text_in_its_turn(
    tmp_path, monkeypatch
):
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png())
    notes = tmp_path / "notes.txt"
    notes.write_text("не картинка", encoding="utf-8")
    gate = _gate_file_check(monkeypatch)
    _clipboard(monkeypatch)
    async with _app(tmp_path).run_test() as pilot:
        inp = _input(pilot)
        try:
            inp._handle_paste(str(shot))
            inp._handle_paste(str(notes))
            await pilot.pause()
        finally:
            gate.set()
        await _settle(pilot)
        # Порядок вставок сохранён; пробел за плашкой отделяет вставленный следом путь.
        assert inp.text == f"[Изображение #1] {notes}"


@pytest.mark.asyncio
async def test_double_esc_cancels_the_queued_paths_too(tmp_path, monkeypatch):
    release = threading.Event()
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png())
    _slow_clipboard(monkeypatch, release, _png())
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        inp = _input(pilot)
        try:
            await pilot.press("alt+v")
            inp._handle_paste(str(shot))
            await pilot.pause()
            await pilot.press("escape")
            await pilot.press("escape")
            await pilot.pause()
            assert scr._image_paste_busy is False
        finally:
            release.set()
        await pilot.pause(0.2)
        assert inp.text == ""  # отменена вся вставка, с очередью


# --- номер плашки ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_chip_number_continues_the_thread_sequence(
    tmp_path, monkeypatch
):
    tid = "imgseq0000000000000000000000001a"
    _write_thread(tmp_path, tid, image_seq=7)
    colors = iter([(1, 1, 1), (2, 2, 2)])
    _clipboard(monkeypatch)
    monkeypatch.setattr(
        os_ops, "read_clipboard_image", lambda: _png(color=next(colors))
    )
    async with _app(tmp_path).run_test() as pilot:
        assert pilot.app.screen.attach_active_task(tid) is True
        for _ in range(2):
            await pilot.press("alt+v")
            await _settle(pilot)
        # meta ещё на 7 (сообщение не отправлено): второй номер — от выданного в процессе
        assert _input(pilot).text == "[Изображение #8] [Изображение #9]"


@pytest.mark.asyncio
async def test_the_chip_number_is_taken_after_the_read(tmp_path, monkeypatch):
    """Пока шло чтение, открыли другую сессию: плашка получает номер её треда."""
    tid = "imgseq0000000000000000000000002b"
    _write_thread(tmp_path, tid, image_seq=7)
    release = threading.Event()
    _slow_clipboard(monkeypatch, release, _png())
    async with _app(tmp_path).run_test() as pilot:
        try:
            await pilot.press("alt+v")
            assert (
                pilot.app.screen.attach_active_task(tid, is_resume=True)
                is True
            )
        finally:
            release.set()
        await _settle(pilot)
        assert _input(pilot).text == "[Изображение #8]"


@pytest.mark.asyncio
async def test_next_image_number_rules(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        assert (
            scr._next_image_number() == 1
        )  # треда нет, ничего не выдано — с 1
        scr._image_seq_issued[None] = (
            2  # выдано в процессе, до появления треда…
        )
        scr._begin_auto_created_task()  # …тред автосоздан, meta ещё нет
        assert scr._next_image_number() == 3


@pytest.mark.asyncio
async def test_returning_to_a_thread_does_not_reissue_its_numbers(
    tmp_path, monkeypatch
):
    """Счётчик выданных номеров — у каждого треда свой: картинка #8 ушла в тред A, а meta
    ещё на 7 (ход идёт); сходили в B и вернулись — следующая в A уже #9."""
    tid_a, tid_b = "imgseqa" + "0" * 25, "imgseqb" + "0" * 25
    _write_thread(tmp_path, tid_a, image_seq=7)
    _write_thread(tmp_path, tid_b, image_seq=3)
    colors = iter([(1, 1, 1), (2, 2, 2)])
    _clipboard(monkeypatch)
    monkeypatch.setattr(
        os_ops, "read_clipboard_image", lambda: _png(color=next(colors))
    )
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        inp = _input(pilot)
        assert scr.attach_active_task(tid_a, is_resume=True) is True
        await pilot.press("alt+v")
        await _settle(pilot)
        assert inp.text == "[Изображение #8]"
        inp.text = ""  # отправлено (meta треда — позже)
        assert scr.attach_active_task(tid_b, is_resume=True) is True
        assert scr.attach_active_task(tid_a, is_resume=True) is True
        await pilot.press("alt+v")
        await _settle(pilot)
        assert inp.text == "[Изображение #9]"


@pytest.mark.asyncio
async def test_the_same_screenshot_pasted_twice_keeps_one_number(
    tmp_path, monkeypatch
):
    colors = iter([(1, 1, 1), (1, 1, 1), (2, 2, 2)])
    _clipboard(monkeypatch)
    monkeypatch.setattr(
        os_ops, "read_clipboard_image", lambda: _png(color=next(colors))
    )
    async with _app(tmp_path).run_test() as pilot:
        for _ in range(3):
            await pilot.press("alt+v")
            await _settle(pilot)
        assert (
            _input(pilot).text
            == "[Изображение #1] [Изображение #1] [Изображение #2]"
        )


@pytest.mark.asyncio
async def test_every_issued_chip_moves_the_counter(tmp_path, monkeypatch):
    a = tmp_path / "a.png"
    a.write_bytes(_png(color=(1, 2, 3)))
    b = tmp_path / "b.png"
    b.write_bytes(_png(color=(4, 5, 6)))
    _clipboard(monkeypatch, image=[str(a), str(b)])
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("alt+v")
        await _settle(pilot)
        assert pilot.app.screen._image_seq_issued == {
            None: 2
        }  # две плашки — счётчик на последней


# --- карман процесса ------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_start_sweeps_dead_pockets_and_exit_clears_its_own(
    tmp_path, monkeypatch
):
    """Карман картинок: старт TUI фоном убирает карманы мёртвых процессов, выход — свой.
    Больше нигде: ни отправка, ни двойной Esc файлы кармана не трогают."""
    from gc_core.attachments.store import ImageStore

    calls = []

    def sweep(self):
        calls.append(
            (
                "sweep",
                self.root,
                threading.current_thread() is threading.main_thread(),
            )
        )

    monkeypatch.setattr(ImageStore, "sweep_dead_staging", sweep)
    monkeypatch.setattr(
        ImageStore,
        "clear_own_staging",
        lambda self: calls.append(("clear", self.root)),
    )
    root = ImageStore.for_threads_dir(str(tmp_path)).root
    async with _app(tmp_path).run_test() as pilot:
        pilot.app._image_sweep.join(5)
        assert calls == [("sweep", root, False)]
    assert calls == [("sweep", root, False), ("clear", root)]


@pytest.mark.asyncio
async def test_an_emergency_exit_clears_the_pocket_through_atexit(
    tmp_path, monkeypatch
):
    """Смерть терминала: аварийный выход (`terminal_guard`) идёт мимо `on_unmount`, но
    хуки atexit выполняет (это доказывает его pty-тест) — карман снимает хук, который
    TUI регистрирует на старте. Штатный выход снимает карман сам и хук убирает."""
    import types

    from gc_core.attachments.store import ImageStore
    from gc_tui import app as app_module

    hooks: list = []
    fake_atexit = types.SimpleNamespace(
        register=hooks.append, unregister=hooks.remove
    )
    monkeypatch.setattr(app_module, "atexit", fake_atexit)
    pocket = Path(ImageStore.for_threads_dir(str(tmp_path)).staging_dir)
    async with _app(tmp_path).run_test() as pilot:
        pilot.app._image_sweep.join(5)
        pocket.mkdir(parents=True, exist_ok=True)
        (pocket / "0123456789abcdef.png").write_bytes(_png())
        [hook] = hooks
        hook()  # аварийный выход: только atexit
        assert not pocket.exists()
    assert hooks == []  # штатный выход хук снял


@pytest.mark.asyncio
async def test_an_app_without_a_threads_dir_keeps_off_the_real_temp_pockets(
    tmp_path,
):
    """Без каталога тредов корень картинок — `<temp>/gc-images-<uid>`, общий у
    пользователя: старт подметает там карманы мёртвых процессов, выход — свой. Тесты
    работают в своём корне, а не в каталоге разработчика."""
    async with TuiApp().run_test() as pilot:
        pilot.app._image_sweep.join(5)
        assert Path(pilot.app.image_store.root).is_relative_to(tmp_path)


@pytest.mark.asyncio
async def test_double_esc_clears_the_chips_but_keeps_the_pocket_file(
    tmp_path, monkeypatch
):
    """Сообщение, отправленное посреди запуска, ядро коммитит позже и берёт файл из
    кармана — двойной Esc сбрасывает записи плашек, но не файлы (решение ведущего плана)."""
    _clipboard(monkeypatch, image=_png())
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("alt+v")
        await _settle(pilot)
        inp = _input(pilot)
        (att,) = inp.images_in_buffer()
        await pilot.press("escape")
        await pilot.press("escape")
        await pilot.pause()
        assert inp.text == ""
        assert (
            inp._chips.images_in_text("[Изображение #1]") == []
        )  # записи плашек сброшены
        assert Path(att.path).is_file()  # файл кармана на месте
