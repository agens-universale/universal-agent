"""Совет о вставке картинки из буфера (визуальный контракт картинок §2): клавиша — та,
что на этой системе действительно вставит то, что лежит в буфере; вставлять нечего —
совета нет. Буфер ОС подменён."""

import io

import pytest
from PIL import Image

from gc_tui.services import image_hints, os_ops
from gc_tui.services.os_ops import ClipboardProbe


def _png_file(path) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (4, 3)).save(buf, format="PNG")
    path.write_bytes(buf.getvalue())
    return str(path)


@pytest.mark.parametrize(
    "platform, text, key",
    [
        ("darwin", False, "Ctrl+V"),  # только картинка: Ctrl+V её и берёт
        ("darwin", True, "Option+V"),  # текст и картинка: Ctrl+V вставит текст
        ("win32", False, "Alt+V"),  # Ctrl+V забирает сам терминал
        ("win32", True, "Alt+V"),
        ("linux", False, "Alt+V"),
    ],
)
def test_an_image_in_the_clipboard(platform, text, key):
    probe = ClipboardProbe(stamp="1", image=True, text=text)
    assert image_hints.clipboard_tip(probe, platform=platform) == (
        f"в буфере изображение · {key} — вставить"
    )


@pytest.mark.parametrize(
    "platform, key", [("darwin", "Ctrl+V"), ("win32", "Alt+V")]
)
def test_copied_image_files(tmp_path, platform, key):
    files = tuple(_png_file(tmp_path / f"{i}.png") for i in range(3))
    tip = image_hints.clipboard_tip(
        ClipboardProbe(stamp="1", files=files), platform=platform
    )
    assert tip == f"в буфере 3 изображения · {key} — вставить"


@pytest.mark.parametrize(
    "count, what",
    [
        (1, "в буфере изображение"),
        (5, "в буфере 5 изображений"),
        (21, "в буфере 21 изображение"),
    ],
)
def test_the_number_of_copied_files_is_spelled_out(tmp_path, count, what):
    files = tuple(_png_file(tmp_path / f"{i}.png") for i in range(count))
    tip = image_hints.clipboard_tip(
        ClipboardProbe(stamp="1", files=files), platform="darwin"
    )
    assert tip == f"{what} · Ctrl+V — вставить"


def test_files_with_a_non_image_get_no_tip(tmp_path):
    """Вставка берёт скопированные файлы все или ни одного — совет не обещает лишнего."""
    note = tmp_path / "notes.txt"
    note.write_text("x", encoding="utf-8")
    files = (_png_file(tmp_path / "a.png"), str(note))
    assert (
        image_hints.clipboard_tip(
            ClipboardProbe(stamp="1", files=files), platform="darwin"
        )
        is None
    )


@pytest.mark.parametrize(
    "probe",
    [
        ClipboardProbe(stamp="1"),
        ClipboardProbe(stamp="1", text=True),
    ],
)
def test_nothing_to_paste_as_an_image_gets_no_tip(probe):
    assert image_hints.clipboard_tip(probe, platform="darwin") is None


def test_read_clipboard_tip_pairs_the_stamp_with_the_tip(monkeypatch):
    monkeypatch.setattr(
        os_ops,
        "probe_clipboard_image",
        lambda: ClipboardProbe(stamp="7", image=True),
    )
    monkeypatch.setattr(image_hints.sys, "platform", "win32")
    assert image_hints.read_clipboard_tip() == (
        "7",
        "в буфере изображение · Alt+V — вставить",
    )

    monkeypatch.setattr(
        os_ops,
        "probe_clipboard_image",
        lambda: ClipboardProbe(stamp="8", text=True),
    )
    assert image_hints.read_clipboard_tip() is None

    monkeypatch.setattr(os_ops, "probe_clipboard_image", lambda: None)
    assert image_hints.read_clipboard_tip() is None
