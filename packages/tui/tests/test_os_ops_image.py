"""os_ops: изображение из буфера обмена и открытие файла. Слой ОС подменён (subprocess,
Pillow ImageGrab, os.startfile): настоящий буфер и просмотрщик тесты не трогают."""

import io
import logging
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image, ImageGrab

from gc_tui.services import os_ops


@pytest.fixture(autouse=True)
def _no_clipboard_probe():
    """Здесь проверяется само чтение буфера: подмена набора (conftest) снята, слой ОС
    подменяет каждый тест."""


def _png(w: int = 4, h: int = 3) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _osascript(kind: str, data: bytes) -> bytes:
    """Вывод osascript для данных буфера: «data PNGf<HEX>» и перевод строки."""
    return f"«data {kind}{data.hex().upper()}»\n".encode("utf-8")


def _done(stdout: bytes = b"", returncode: int = 0):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=b"")


def _no_process(*_args, **_kwargs):
    raise AssertionError("дочерний процесс здесь не ожидается")


def _spy_popen(seen: dict):
    def fake_popen(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return SimpleNamespace(pid=1)

    return fake_popen


# --- macOS -------------------------------------------------------------------


def test_macos_reads_png_with_osascript_detached_from_the_console(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "darwin")
    png = _png()
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return _done(_osascript("PNGf", png))

    monkeypatch.setattr(os_ops.subprocess, "run", fake_run)
    assert os_ops.read_clipboard_image() == png
    assert seen["cmd"][:2] == ["osascript", "-e"]
    assert (
        "«class PNGf»" in seen["cmd"][2] and "«class TIFF»" in seen["cmd"][2]
    )
    assert seen["kw"]["stdin"] is subprocess.DEVNULL
    assert seen["kw"]["capture_output"] is True
    assert seen["kw"]["timeout"] == 5.0


def test_macos_finder_copied_files_come_as_paths(monkeypatch):
    """Finder «Скопировать»: в буфере файлы и их значок (TIFF) — берутся пути файлов
    (как CF_HDROP на Windows), а не значок. Пути — раньше PNGf/TIFF в скрипте."""
    monkeypatch.setattr(os_ops.sys, "platform", "darwin")
    seen = {}

    def fake_run(cmd, **kw):
        seen["script"] = cmd[2]
        return _done(
            "/Users/me/a b.png\n/Users/me/Снимок экрана.png\n".encode("utf-8")
        )

    monkeypatch.setattr(os_ops.subprocess, "run", fake_run)
    assert os_ops.read_clipboard_image() == [
        "/Users/me/a b.png",
        "/Users/me/Снимок экрана.png",
    ]
    script = seen["script"]
    assert "NSPasteboardURLReadingFileURLsOnlyKey" in script
    assert (
        script.index("NSURL")
        < script.index("«class PNGf»")
        < script.index("«class TIFF»")
    )


def test_macos_falls_back_to_tiff(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "darwin")
    tiff = b"MM\x00*" + b"\x00" * 8
    monkeypatch.setattr(
        os_ops.subprocess,
        "run",
        lambda cmd, **kw: _done(_osascript("TIFF", tiff)),
    )
    assert os_ops.read_clipboard_image() == tiff


def test_macos_without_an_image_is_none(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "darwin")
    monkeypatch.setattr(
        os_ops.subprocess, "run", lambda cmd, **kw: _done(returncode=1)
    )
    assert os_ops.read_clipboard_image() is None


def test_macos_timeout_is_none(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "darwin")

    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(os_ops.subprocess, "run", hang)
    assert os_ops.read_clipboard_image() is None


def test_macos_unexpected_output_is_none(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "darwin")
    monkeypatch.setattr(
        os_ops.subprocess, "run", lambda cmd, **kw: _done(b"some text\n")
    )
    assert os_ops.read_clipboard_image() is None


# --- Linux / WSLg ------------------------------------------------------------


def test_linux_reads_png_with_wl_paste_first(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "linux")
    png = _png()
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw["stdin"], kw["timeout"]))
        return _done(png)

    monkeypatch.setattr(os_ops.subprocess, "run", fake_run)
    assert os_ops.read_clipboard_image() == png
    assert calls == [
        (["wl-paste", "-t", "image/png"], subprocess.DEVNULL, 5.0)
    ]


def test_linux_falls_back_to_xclip(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "linux")
    png = _png()
    tried = []

    def fake_run(cmd, **kw):
        tried.append(cmd[0])
        if cmd[0] == "wl-paste":
            raise FileNotFoundError(cmd[0])
        return _done(png)

    monkeypatch.setattr(os_ops.subprocess, "run", fake_run)
    assert os_ops.read_clipboard_image() == png
    assert tried == ["wl-paste", "xclip"]


def test_linux_without_wl_paste_and_xclip_raises(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "linux")

    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(os_ops.subprocess, "run", missing)
    with pytest.raises(os_ops.ClipboardToolMissing):
        os_ops.read_clipboard_image()


def test_linux_tool_without_an_image_is_none(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "linux")

    def fake_run(cmd, **kw):
        if cmd[0] == "xclip":
            raise FileNotFoundError(cmd[0])
        return _done(
            returncode=1
        )  # wl-paste: «No suitable type of content copied»

    monkeypatch.setattr(os_ops.subprocess, "run", fake_run)
    assert os_ops.read_clipboard_image() is None


def test_linux_text_read_asks_wayland_for_text_only(monkeypatch):
    """Буфер только с картинкой: wl-paste без --type отдал бы её байты мусорным текстом."""
    monkeypatch.setattr(os_ops.sys, "platform", "linux")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _done(b"", returncode=1)

    monkeypatch.setattr(os_ops.subprocess, "run", fake_run)
    assert os_ops.read_clipboard() == ""
    assert calls[0] == ["wl-paste", "--no-newline", "--type", "text"]


# --- Windows -----------------------------------------------------------------


def test_windows_image_becomes_png_bytes_without_processes(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "win32")
    monkeypatch.setattr(os_ops.subprocess, "run", _no_process)
    monkeypatch.setattr(os_ops.subprocess, "Popen", _no_process)
    monkeypatch.setattr(
        ImageGrab, "grabclipboard", lambda: Image.new("RGB", (5, 4), (1, 2, 3))
    )
    data = os_ops.read_clipboard_image()
    with Image.open(io.BytesIO(data)) as im:
        assert (im.format, im.size) == ("PNG", (5, 4))


def test_windows_copied_files_come_as_paths(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "win32")
    files = [r"C:\Users\me\shot.png", r"C:\Users\me\notes.txt"]
    monkeypatch.setattr(ImageGrab, "grabclipboard", lambda: list(files))
    assert os_ops.read_clipboard_image() == files


def test_windows_clipboard_without_an_image_is_none(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "win32")
    monkeypatch.setattr(ImageGrab, "grabclipboard", lambda: None)
    assert os_ops.read_clipboard_image() is None


def test_windows_busy_clipboard_is_none(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "win32")

    def busy():
        raise OSError("failed to open clipboard")

    monkeypatch.setattr(ImageGrab, "grabclipboard", busy)
    assert os_ops.read_clipboard_image() is None


def test_windows_image_that_cannot_become_png_is_none(monkeypatch):
    """Pillow не сохранит режим CMYK в PNG — это не падение вставки, а «картинки нет»."""
    monkeypatch.setattr(os_ops.sys, "platform", "win32")
    monkeypatch.setattr(
        ImageGrab, "grabclipboard", lambda: Image.new("CMYK", (4, 4))
    )
    assert os_ops.read_clipboard_image() is None


def test_windows_broken_clipboard_data_is_none(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "win32")

    def broken():
        raise ValueError("unsupported BMP header")

    monkeypatch.setattr(ImageGrab, "grabclipboard", broken)
    assert os_ops.read_clipboard_image() is None


# --- что в буфере — для подсказки о вставке -----------------------------------
# Проверка не читает саму картинку (кроме Linux, где метки буфера нет): метка
# состояния буфера и что в нём лежит.


def test_macos_probe_real_output_image_only(monkeypatch):
    """Настоящий вывод скрипта на macOS (2026-09-28): метка changeCount и «image»."""
    monkeypatch.setattr(os_ops.sys, "platform", "darwin")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return _done(b"822\nimage\n")

    monkeypatch.setattr(os_ops.subprocess, "run", fake_run)
    assert os_ops.probe_clipboard_image() == os_ops.ClipboardProbe(
        stamp="822", image=True
    )
    script = seen["cmd"][2]
    assert seen["cmd"][:2] == ["osascript", "-e"]
    assert (
        "changeCount" in script
        and "NSPasteboardURLReadingFileURLsOnlyKey" in script
    )
    assert "«class PNGf»" not in script  # саму картинку не читает
    assert seen["kw"]["stdin"] is subprocess.DEVNULL


def test_macos_probe_image_with_a_link(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "darwin")
    monkeypatch.setattr(
        os_ops.subprocess, "run", lambda cmd, **kw: _done(b"823\nimage text\n")
    )
    assert os_ops.probe_clipboard_image() == os_ops.ClipboardProbe(
        stamp="823", image=True, text=True
    )


def test_macos_probe_text_only(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "darwin")
    monkeypatch.setattr(
        os_ops.subprocess, "run", lambda cmd, **kw: _done(b"824\ntext\n")
    )
    assert os_ops.probe_clipboard_image() == os_ops.ClipboardProbe(
        stamp="824", text=True
    )


def test_macos_probe_finder_files(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "darwin")
    out = (
        "825\nfiles\n/Users/me/a b.png\n/Users/me/Снимок экрана.png\n".encode(
            "utf-8"
        )
    )
    monkeypatch.setattr(os_ops.subprocess, "run", lambda cmd, **kw: _done(out))
    assert os_ops.probe_clipboard_image() == os_ops.ClipboardProbe(
        stamp="825", files=("/Users/me/a b.png", "/Users/me/Снимок экрана.png")
    )


@pytest.mark.parametrize(
    "result", [_done(returncode=1), _done(b""), _done(b"garbage")]
)
def test_macos_probe_failure_is_none(monkeypatch, result):
    monkeypatch.setattr(os_ops.sys, "platform", "darwin")
    monkeypatch.setattr(os_ops.subprocess, "run", lambda cmd, **kw: result)
    assert os_ops.probe_clipboard_image() is None


def test_linux_clipboard_is_not_probed(monkeypatch):
    """Linux буфер без вставки не читает (ревью 2026-10-01): метки буфера нет, проба
    читала бы картинку целиком, а wl-paste в GNOME на Wayland на миг отбирает фокус
    у терминала — и возвращение фокуса запускало бы пробу снова."""
    monkeypatch.setattr(os_ops.sys, "platform", "linux")
    monkeypatch.setattr(os_ops.subprocess, "run", _no_process)
    assert os_ops.probe_clipboard_image() is None


def test_windows_probe_uses_the_clipboard_sequence_number(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "win32")
    monkeypatch.setattr(os_ops.subprocess, "run", _no_process)
    monkeypatch.setattr(os_ops, "_win_clipboard_sequence", lambda: 41)
    monkeypatch.setattr(
        ImageGrab, "grabclipboard", lambda: Image.new("RGB", (5, 4))
    )
    assert os_ops.probe_clipboard_image() == os_ops.ClipboardProbe(
        stamp="41", image=True
    )

    files = [r"C:\Users\me\shot.png"]
    monkeypatch.setattr(ImageGrab, "grabclipboard", lambda: list(files))
    assert os_ops.probe_clipboard_image() == os_ops.ClipboardProbe(
        stamp="41", files=tuple(files)
    )

    monkeypatch.setattr(ImageGrab, "grabclipboard", lambda: None)
    assert os_ops.probe_clipboard_image() == os_ops.ClipboardProbe(stamp="41")


def test_windows_probe_without_access_or_on_a_busy_clipboard_is_none(
    monkeypatch,
):
    monkeypatch.setattr(os_ops.sys, "platform", "win32")
    monkeypatch.setattr(
        os_ops, "_win_clipboard_sequence", lambda: 0
    )  # нет доступа
    assert os_ops.probe_clipboard_image() is None

    def busy():
        raise OSError("failed to open clipboard")

    monkeypatch.setattr(os_ops, "_win_clipboard_sequence", lambda: 41)
    monkeypatch.setattr(ImageGrab, "grabclipboard", busy)
    assert os_ops.probe_clipboard_image() is None


# --- open_path ---------------------------------------------------------------


def test_open_path_macos_opens_detached(monkeypatch, tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png())
    monkeypatch.setattr(os_ops.sys, "platform", "darwin")
    seen = {}
    monkeypatch.setattr(os_ops.subprocess, "Popen", _spy_popen(seen))
    assert os_ops.open_path(str(shot)) is True
    assert seen["cmd"] == ["open", str(shot)]
    for stream in ("stdin", "stdout", "stderr"):
        assert seen["kw"][stream] is subprocess.DEVNULL
    assert seen["kw"]["start_new_session"] is True


def test_open_path_linux_uses_xdg_open(monkeypatch, tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png())
    monkeypatch.setattr(os_ops.sys, "platform", "linux")
    seen = {}
    monkeypatch.setattr(os_ops.subprocess, "Popen", _spy_popen(seen))
    assert os_ops.open_path(str(shot)) is True
    assert seen["cmd"] == ["xdg-open", str(shot)]


def test_open_path_windows_uses_startfile(monkeypatch, tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png())
    monkeypatch.setattr(os_ops.sys, "platform", "win32")
    monkeypatch.setattr(os_ops.subprocess, "Popen", _no_process)
    opened = []
    monkeypatch.setattr(os_ops.os, "startfile", opened.append, raising=False)
    assert os_ops.open_path(str(shot)) is True
    assert opened == [str(shot)]


def test_open_path_missing_file_is_false_without_launching(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(os_ops.sys, "platform", "darwin")
    monkeypatch.setattr(os_ops.subprocess, "Popen", _no_process)
    assert os_ops.open_path(str(tmp_path / "gone.png")) is False


def test_open_path_launcher_failure_is_false_and_logged(
    monkeypatch, tmp_path, caplog
):
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png())
    monkeypatch.setattr(os_ops.sys, "platform", "linux")

    def no_launcher(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(os_ops.subprocess, "Popen", no_launcher)
    with caplog.at_level(logging.WARNING, logger=os_ops.__name__):
        assert os_ops.open_path(str(shot)) is False
    assert "xdg-open" in caplog.text


# --- скопированные файлы — для Ctrl+V ---------------------------------------------
# Finder кладёт рядом с файлами их имена текстом: Ctrl+V сначала спрашивает о файлах.


def test_copied_files_are_the_paths_from_the_probe(monkeypatch):
    monkeypatch.setattr(os_ops.sys, "platform", "darwin")
    out = "826\nfiles\n/Users/me/a.png\n/Users/me/b.png\n".encode("utf-8")
    monkeypatch.setattr(os_ops.subprocess, "run", lambda cmd, **kw: _done(out))
    assert os_ops.read_clipboard_files() == (
        "/Users/me/a.png",
        "/Users/me/b.png",
    )


@pytest.mark.parametrize(
    "result", [_done(b"827\nimage text\n"), _done(returncode=1)]
)
def test_no_copied_files_is_empty(monkeypatch, result):
    monkeypatch.setattr(os_ops.sys, "platform", "darwin")
    monkeypatch.setattr(os_ops.subprocess, "run", lambda cmd, **kw: result)
    assert os_ops.read_clipboard_files() == ()


def test_linux_does_not_read_copied_files(monkeypatch):
    """Проба Linux читает саму картинку, а файлы вставка там не берёт — не зовём."""
    monkeypatch.setattr(os_ops.sys, "platform", "linux")
    monkeypatch.setattr(os_ops.subprocess, "run", _no_process)
    assert os_ops.read_clipboard_files() == ()


@pytest.mark.parametrize(
    "platform, expected",
    [("darwin", True), ("win32", False), ("linux", False)],
)
def test_only_finder_puts_copied_file_names_next_to_the_files(
    monkeypatch, platform, expected
):
    monkeypatch.setattr(os_ops.sys, "platform", platform)
    assert os_ops.copied_files_come_with_names() is expected
