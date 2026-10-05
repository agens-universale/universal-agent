"""Вставка изображения до плашки: путь к файлу во вставленном тексте (перетаскивание
файла в окно терминала, вставка пути), буфер обмена ОС, лимит и карман процесса.
Технический слой — без Textual и UI: синтаксис пути без диска, проверка байтов файла
и сборка вложений; тексты отказов и плашки — у экрана."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from gc_core.attachments.model import ORIGIN_USER, Attachment
from gc_core.attachments.normalize import (
    REASON_NOT_AN_IMAGE,
    REASON_TOO_LARGE,
    SNIFF_BYTES,
    NormalizeError,
    looks_like_image,
)
from gc_core.attachments.store import ImageStore, amake_attachment

from gc_tui.services import os_ops

_log = logging.getLogger(__name__)

_QUOTES = "'\""
_ESCAPED_CHAR = re.compile(r"\\(.)")
_WINDOWS_DRIVE = re.compile(r"^([A-Za-z]):[\\/]")
_FILE_URL_DRIVE = re.compile(r"^/[A-Za-z]:")

#: Потолок всей вставки — чтение буфера или файла, проверка, нормализация, карман:
#: зависший клипборд-менеджер или сетевой диск не держат поле ввода дольше.
PASTE_TIMEOUT_SECONDS = 15.0

# Причины отказа вставки (`PasteRefusal.reason`); «не изображение» и «слишком
# большое» — причины нормализации ядра (REASON_NOT_AN_IMAGE, REASON_TOO_LARGE).
REFUSE_NO_TOOL = "no_tool"
REFUSE_LIMIT = "limit"
REFUSE_SAVE_FAILED = "save_failed"
REFUSE_TIMEOUT = "timeout"


@dataclass(frozen=True)
class PasteRefusal:
    """Вставка изображения не удалась; `reason` — REFUSE_* или причина нормализации.
    Текст отказа выбирает экран."""

    reason: str


def pasted_file_path(text: str) -> str | None:
    """Абсолютный путь, если вставка — один путь к файлу; иначе None. Только синтаксис,
    диск не читается. Кавычки по краям снимаются, `file://` раскодируется; вне Windows
    снимается `\\`-экранирование, а путь Windows (`C:\\…`, вставленный в WSL) уходит в
    `/mnt/<диск>/…`. По пробельным строку не режем: U+202F в именах скриншотов macOS —
    тоже пробельный символ."""
    s = text.strip()
    if not s or "\n" in s:
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in _QUOTES:
        s = s[1:-1]
    if s.startswith("file://"):
        s = unquote(urlparse(s).path)
        if sys.platform == "win32" and _FILE_URL_DRIVE.match(
            s
        ):  # file:///C:/…
            s = s[1:]
    elif sys.platform != "win32":
        drive = _WINDOWS_DRIVE.match(s)
        if drive:
            s = f"/mnt/{drive.group(1).lower()}/" + s[3:].replace("\\", "/")
        else:
            s = _ESCAPED_CHAR.sub(r"\1", s)
    s = os.path.expanduser(s)
    return s if os.path.isabs(s) else None


def is_image_file(path: str) -> bool:
    """Обычный файл, и его первые байты — сигнатура поддерживаемого формата. Каталоги,
    устройства и каналы — нет (чтение не повиснет). Читает диск — звать вне UI-потока."""
    try:
        if not os.path.isfile(path):
            return False
        with open(path, "rb") as fh:
            head = fh.read(SNIFF_BYTES)
    except OSError:
        return False
    return looks_like_image(head)


def _copied_image_files() -> tuple[str, ...]:
    """Скопированные файлы, если все — картинки; иначе пусто."""
    files = os_ops.read_clipboard_files()
    return files if all(is_image_file(p) for p in files) else ()


async def astage_paste(
    path: str | None,
    store: ImageStore,
    *,
    room: int,
    strict: bool,
    files_only: bool = False,
) -> list[Attachment] | PasteRefusal | None:
    """Изображения вставки — в карман процесса (спека картинок §12).

    `path` — файл-кандидат из вставленного текста, None — буфер обмена ОС. Итог:
    вложения (подпись под номер ставит стор плашек), отказ или None — изображения нет
    (вызывающий вставит текст или откажет «нет изображения»). `room` — сколько картинок
    ещё влезает в сообщение; лимит проверяется, только когда изображение найдено, и
    не влезло — отказ `REFUSE_LIMIT` при любом жесте (запасную вставку делает экран).
    `strict` — жест просил именно изображение (Alt+V, пустая вставка): нечем читать
    буфер — отказ; иначе (Ctrl+V, правый клик, путь) это «изображения нет».
    `files_only` — из буфера только скопированные файлы-картинки (Ctrl+V при тексте
    в буфере: Finder кладёт рядом с файлами их имена); не они — «изображения нет»,
    картинку данными не читает — её рядом с текстом берёт Alt+V.
    Скопированные файлы — все или ни одного. Диск, буфер и Pillow — вне потока цикла;
    вся вставка — не дольше PASTE_TIMEOUT_SECONDS."""
    try:
        return await asyncio.wait_for(
            _astage_paste(
                path, store, room=room, strict=strict, files_only=files_only
            ),
            PASTE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return PasteRefusal(REFUSE_TIMEOUT)


async def _astage_paste(
    path: str | None,
    store: ImageStore,
    *,
    room: int,
    strict: bool,
    files_only: bool,
) -> list[Attachment] | PasteRefusal | None:
    if path is not None:
        sources: list[bytes | str] = (
            [path] if await asyncio.to_thread(is_image_file, path) else []
        )
    elif files_only:
        sources = list(await asyncio.to_thread(_copied_image_files))
    else:
        try:
            grabbed = await asyncio.to_thread(os_ops.read_clipboard_image)
        except os_ops.ClipboardToolMissing:
            return PasteRefusal(REFUSE_NO_TOOL) if strict else None
        if isinstance(grabbed, list):
            if not await asyncio.to_thread(
                lambda: all(is_image_file(p) for p in grabbed)
            ):
                return PasteRefusal(REASON_NOT_AN_IMAGE)
            sources = list(grabbed)
        else:
            sources = [grabbed] if grabbed else []
    if not sources:
        return None
    if len(sources) > room:
        return PasteRefusal(REFUSE_LIMIT)
    staged: list[Attachment] = []
    for src in sources:
        try:
            staged.append(
                await amake_attachment(
                    store, src, origin=ORIGIN_USER, label=""
                )
            )
        except NormalizeError as exc:
            return PasteRefusal(
                REASON_TOO_LARGE
                if exc.reason == REASON_TOO_LARGE
                else REASON_NOT_AN_IMAGE
            )
        except OSError:
            # Карман не записался (диск полон, нет прав) или файл пропал после проверки.
            _log.warning("image paste: cannot stage the image", exc_info=True)
            return PasteRefusal(REFUSE_SAVE_FAILED)
    return staged
