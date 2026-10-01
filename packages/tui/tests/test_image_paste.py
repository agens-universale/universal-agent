"""image_paste: путь к файлу-изображению во вставленном тексте — синтаксис без диска
и проверка байтов файла; вставка изображения до плашки — источник, лимит, карман."""

import io
import sys
import threading
from pathlib import Path
from urllib.parse import quote

import pytest
from PIL import Image

from gc_core.attachments.model import (
    CLIPBOARD_SOURCE,
    MAX_INPUT_BYTES,
    ORIGIN_USER,
)
from gc_core.attachments.normalize import REASON_NOT_AN_IMAGE, REASON_TOO_LARGE
from gc_core.attachments.store import ImageStore
from gc_tui.services import image_paste, os_ops
from gc_tui.services.image_paste import (
    REFUSE_LIMIT,
    REFUSE_NO_TOOL,
    REFUSE_SAVE_FAILED,
    REFUSE_TIMEOUT,
    PasteRefusal,
    astage_paste,
    is_image_file,
    pasted_file_path,
)

#: Имя скриншота macOS: пробелы, кириллица и узкий неразрывный пробел U+202F.
SHOT = "Снимок экрана 2026-09-24 в\u202f10.00.00.png"
BASE = "/Users/me/Desktop"


def test_plain_absolute_path_is_a_candidate():
    # U+202F — пробельный символ: строку не режем по пробельным, имя целиком
    assert pasted_file_path(f"{BASE}/{SHOT}") == f"{BASE}/{SHOT}"


def test_terminal_padding_is_dropped():
    # Terminal.app дописывает пробел после перетащенного пути
    assert pasted_file_path(f"  {BASE}/{SHOT} \n") == f"{BASE}/{SHOT}"


@pytest.mark.parametrize("mark", ["'", '"'])
def test_quotes_are_stripped(mark):
    assert pasted_file_path(f"{mark}{BASE}/{SHOT}{mark}") == f"{BASE}/{SHOT}"


def test_file_url_is_decoded():
    assert (
        pasted_file_path("file://" + quote(f"{BASE}/{SHOT}"))
        == f"{BASE}/{SHOT}"
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="\\-экранирование снимается только вне Windows",
)
def test_backslash_escapes_are_removed():
    escaped = f"{BASE}/{SHOT}".replace(" ", "\\ ")
    assert pasted_file_path(escaped + " ") == f"{BASE}/{SHOT}"


@pytest.mark.skipif(
    sys.platform == "win32", reason="путь Windows, вставленный в WSL"
)
def test_windows_drive_path_maps_to_mnt():
    assert (
        pasted_file_path("C:\\Users\\me\\Снимок 1.png")
        == "/mnt/c/Users/me/Снимок 1.png"
    )


def test_tilde_expands(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert Path(pasted_file_path("~/shot.png")) == tmp_path / "shot.png"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "README.md",
        f"{BASE}/a.png\n{BASE}/b.png",
        "https://example.com/cat.png",
    ],
)
def test_not_a_single_absolute_path(text):
    assert pasted_file_path(text) is None


def test_an_image_file_is_recognised_by_its_bytes(tmp_path):
    shot = tmp_path / "без расширения"  # формат — по байтам, не по имени
    Image.new("RGB", (40, 40), (0, 128, 255)).save(shot, format="PNG")
    assert is_image_file(str(shot)) is True


def test_everything_else_is_not_an_image_file(tmp_path):
    notes = tmp_path / "notes.png"
    notes.write_text("это текст", encoding="utf-8")
    short = tmp_path / "short.png"
    short.write_bytes(b"\x89P")
    assert is_image_file(str(notes)) is False
    assert is_image_file(str(short)) is False
    assert is_image_file(str(tmp_path)) is False
    assert is_image_file(str(tmp_path / "missing.png")) is False


# --- astage_paste: источник → проверки → карман (без Textual) --------------------


def _png(color=(10, 20, 30), size=(40, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _clipboard(monkeypatch, grabbed) -> None:
    """Слой ОС подменён: настоящий буфер обмена разработчика тест не читает."""

    def read():
        if isinstance(grabbed, BaseException):
            raise grabbed
        return grabbed

    monkeypatch.setattr(os_ops, "read_clipboard_image", read)


def _store(tmp_path) -> ImageStore:
    return ImageStore(str(tmp_path / "images"))


@pytest.mark.asyncio
async def test_clipboard_image_is_staged_without_a_number(
    tmp_path, monkeypatch
):
    """Подпись под номер ставит стор плашек — сервис номеров не выдаёт."""
    _clipboard(monkeypatch, _png())
    store = _store(tmp_path)
    (att,) = await astage_paste(None, store, room=10, strict=True)
    assert (att.origin, att.source, att.label) == (
        ORIGIN_USER,
        CLIPBOARD_SOURCE,
        "",
    )
    assert Path(att.path).parent == Path(store.staging_dir)


@pytest.mark.asyncio
async def test_pasted_image_path_is_staged_with_its_full_path(
    tmp_path, monkeypatch
):
    _clipboard(monkeypatch, AssertionError("путь не читает буфер"))
    shot = tmp_path / SHOT
    shot.write_bytes(_png())
    (att,) = await astage_paste(
        str(shot), _store(tmp_path), room=10, strict=False
    )
    assert att.source == str(shot)


@pytest.mark.asyncio
async def test_no_image_is_none(tmp_path, monkeypatch):
    notes = tmp_path / "notes.txt"
    notes.write_text("текст", encoding="utf-8")
    _clipboard(monkeypatch, None)
    assert (
        await astage_paste(None, _store(tmp_path), room=10, strict=True)
        is None
    )
    assert (
        await astage_paste(str(notes), _store(tmp_path), room=10, strict=False)
        is None
    )


@pytest.mark.asyncio
async def test_missing_clipboard_tool_refuses_only_an_explicit_image_request(
    tmp_path, monkeypatch
):
    """Alt+V и пустая вставка просили изображение — отказ; Ctrl+V и правый клик — «нет
    изображения», и вызывающий вставит свой буфер."""
    _clipboard(
        monkeypatch, os_ops.ClipboardToolMissing("нет ни wl-paste, ни xclip")
    )
    assert await astage_paste(
        None, _store(tmp_path), room=10, strict=True
    ) == (PasteRefusal(REFUSE_NO_TOOL))
    assert (
        await astage_paste(None, _store(tmp_path), room=10, strict=False)
        is None
    )


@pytest.mark.asyncio
async def test_full_limit_refuses_any_found_image(tmp_path, monkeypatch):
    """Не влезла — отказ при любом жесте; запасную вставку нестрогого жеста делает экран."""
    _clipboard(monkeypatch, _png())
    assert await astage_paste(None, _store(tmp_path), room=0, strict=True) == (
        PasteRefusal(REFUSE_LIMIT)
    )
    assert await astage_paste(
        None, _store(tmp_path), room=0, strict=False
    ) == (PasteRefusal(REFUSE_LIMIT))


@pytest.mark.asyncio
async def test_limit_is_checked_only_after_an_image_is_found(
    tmp_path, monkeypatch
):
    """Полный лимит, а в буфере текст: это «изображения нет» (Alt+V вставит текст)."""
    _clipboard(monkeypatch, None)
    assert (
        await astage_paste(None, _store(tmp_path), room=0, strict=True) is None
    )


@pytest.mark.asyncio
async def test_copied_files_are_all_or_nothing(tmp_path, monkeypatch):
    a, b, c = (tmp_path / f"{n}.png" for n in "abc")
    for i, f in enumerate((a, b, c)):
        f.write_bytes(_png(color=(i, i, i)))
    notes = tmp_path / "notes.txt"
    notes.write_text("текст", encoding="utf-8")
    store = _store(tmp_path)
    _clipboard(monkeypatch, [str(a), str(notes)])
    assert await astage_paste(
        None, store, room=10, strict=True
    ) == PasteRefusal(REASON_NOT_AN_IMAGE)
    _clipboard(monkeypatch, [str(a), str(b), str(c)])
    assert await astage_paste(
        None, store, room=2, strict=True
    ) == PasteRefusal(REFUSE_LIMIT)
    _clipboard(monkeypatch, [str(a), str(b)])
    assert [
        x.source for x in await astage_paste(None, store, room=2, strict=True)
    ] == [str(a), str(b)]


def _copied_files(monkeypatch, files) -> None:
    """Скопированные файлы в буфере; картинку данными режим «только файлы» не читает."""
    monkeypatch.setattr(os_ops, "read_clipboard_files", lambda: tuple(files))
    monkeypatch.setattr(
        os_ops,
        "read_clipboard_image",
        lambda: pytest.fail("картинку данными не читает"),
    )


@pytest.mark.asyncio
async def test_files_only_stages_copied_image_files(tmp_path, monkeypatch):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(_png(color=(1, 1, 1)))
    b.write_bytes(_png(color=(2, 2, 2)))
    _copied_files(monkeypatch, [str(a), str(b)])
    staged = await astage_paste(
        None, _store(tmp_path), room=10, strict=False, files_only=True
    )
    assert [x.source for x in staged] == [str(a), str(b)]


@pytest.mark.asyncio
async def test_files_only_without_image_files_is_nothing(
    tmp_path, monkeypatch
):
    """Среди файлов не картинка или файлов нет — «изображения нет»: вызывающий
    вставит текст (имена файлов, которые Finder кладёт рядом)."""
    a = tmp_path / "a.png"
    a.write_bytes(_png())
    notes = tmp_path / "notes.txt"
    notes.write_text("текст", encoding="utf-8")
    store = _store(tmp_path)
    _copied_files(monkeypatch, [str(a), str(notes)])
    assert (
        await astage_paste(None, store, room=10, strict=False, files_only=True)
        is None
    )
    _copied_files(monkeypatch, [])
    assert (
        await astage_paste(None, store, room=10, strict=False, files_only=True)
        is None
    )


@pytest.mark.asyncio
async def test_files_only_over_the_limit_is_refused(tmp_path, monkeypatch):
    files = [tmp_path / f"{n}.png" for n in "ab"]
    for i, f in enumerate(files):
        f.write_bytes(_png(color=(i, i, i)))
    _copied_files(monkeypatch, [str(f) for f in files])
    assert await astage_paste(
        None, _store(tmp_path), room=1, strict=False, files_only=True
    ) == PasteRefusal(REFUSE_LIMIT)


@pytest.mark.asyncio
async def test_bad_bytes_are_refused_by_reason(tmp_path, monkeypatch):
    _clipboard(monkeypatch, b"definitely not an image")
    assert await astage_paste(
        None, _store(tmp_path), room=10, strict=True
    ) == (PasteRefusal(REASON_NOT_AN_IMAGE))
    _clipboard(monkeypatch, b"\0" * (MAX_INPUT_BYTES + 1))
    assert await astage_paste(
        None, _store(tmp_path), room=10, strict=True
    ) == (PasteRefusal(REASON_TOO_LARGE))


@pytest.mark.asyncio
async def test_unwritable_pocket_is_a_save_failure_not_a_bad_image(
    tmp_path, monkeypatch
):
    """Карман не записался (диск полон, нет прав) — «не удалось сохранить», а не
    «это не изображение»."""
    blocker = tmp_path / "images"
    blocker.write_text("файл на месте каталога", encoding="utf-8")
    _clipboard(monkeypatch, _png())
    assert await astage_paste(
        None, ImageStore(str(blocker)), room=10, strict=True
    ) == (PasteRefusal(REFUSE_SAVE_FAILED))


@pytest.mark.asyncio
async def test_a_hung_read_times_out(tmp_path, monkeypatch):
    release = threading.Event()

    def hang():
        release.wait(5)
        return None

    monkeypatch.setattr(os_ops, "read_clipboard_image", hang)
    monkeypatch.setattr(image_paste, "PASTE_TIMEOUT_SECONDS", 0.05)
    try:
        assert await astage_paste(
            None, _store(tmp_path), room=10, strict=True
        ) == (PasteRefusal(REFUSE_TIMEOUT))
    finally:
        release.set()
