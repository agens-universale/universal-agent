"""Нормализация изображения перед хранением и отправкой модели.

Одна форма на выходе для всех источников (буфер обмена, файл, тул): RGB или
оттенки серого, длинная сторона не больше ``MAX_LONG_EDGE``, короткая — не
меньше ``MIN_SHORT_EDGE``, PNG или JPEG. Синхронная CPU-работа: вызывающие зовут
``normalize_image`` через ``asyncio.to_thread``. Pillow импортируется внутри
функций — импорт ядра за него не платит (сторож —
``tests/attachments/test_attachments_import_light.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING

from gc_core.attachments.model import (
    ALLOWED_FORMATS,
    JPEG_THRESHOLD,
    MAX_INPUT_BYTES,
    MAX_LONG_EDGE,
    MAX_PIXELS,
    MIN_SHORT_EDGE,
)

if TYPE_CHECKING:
    from PIL.Image import Image

MIME_PNG = "image/png"
MIME_JPEG = "image/jpeg"
#: Сколько первых байт файла нужно ``looks_like_image``.
SNIFF_BYTES = 16
#: ``NormalizeError.reason``: не картинка допустимого формата / слишком большая.
REASON_NOT_AN_IMAGE = "not_an_image"
REASON_TOO_LARGE = "too_large"

_JPEG_QUALITY = 85
_WHITE = (255, 255, 255)
_EXIF_ORIENTATION = 0x0112
_ALPHA_MODES = frozenset({"RGBA", "LA", "PA", "RGBa", "La"})


@dataclass(frozen=True)
class NormalizedImage:
    data: bytes
    mime: str
    width: int
    height: int
    orig_width: int
    orig_height: int


class NormalizeError(ValueError):
    """Отказ нормализации; ``reason`` — ``REASON_NOT_AN_IMAGE`` | ``REASON_TOO_LARGE``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def looks_like_image(head: bytes) -> bool:
    """Похоже ли начало файла на картинку допустимого формата — по сигнатуре.

    Дёшево и без Pillow: решает, стоит ли вообще нормализовать вставленный
    путь. ``head`` — первые ``SNIFF_BYTES`` байт. У BMP сигнатура из двух букв,
    поэтому дополнительно сверяются нулевые зарезервированные поля заголовка.
    """
    return (
        head.startswith(_PREFIX_SIGNATURES)
        or (head.startswith(b"RIFF") and head[8:12] == b"WEBP")
        or (head.startswith(b"BM") and head[6:10] == b"\x00\x00\x00\x00")
    )


#: Сигнатуры, которым хватает префикса: PNG, JPEG, GIF, TIFF (оба порядка байт).
_PREFIX_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"II*\x00",
    b"MM\x00*",
)


def normalize_image(data: bytes) -> NormalizedImage:
    """Байты картинки → нормализованная картинка; отказ — ``NormalizeError``.

    ``orig_width``/``orig_height`` — размер, каким его видит человек: после
    поворота по EXIF, до ужатия.
    """
    if len(data) > MAX_INPUT_BYTES:
        raise NormalizeError(REASON_TOO_LARGE)
    from PIL import Image

    try:
        img = Image.open(BytesIO(data), formats=ALLOWED_FORMATS)
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise NormalizeError(REASON_TOO_LARGE) from exc
    except (OSError, ValueError, SyntaxError, EOFError) as exc:
        raise NormalizeError(REASON_NOT_AN_IMAGE) from exc
    width, height = img.size
    if width * height > MAX_PIXELS:
        raise NormalizeError(REASON_TOO_LARGE)
    try:
        return _normalize_opened(img, data)
    except (OSError, ValueError, SyntaxError, EOFError) as exc:
        raise NormalizeError(REASON_NOT_AN_IMAGE) from exc


#: Сегменты APPn, которые мелкий JPEG может нести как есть: JFIF (APP0), цветовой
#: профиль (APP2), Adobe (APP14). Прочие APPn (EXIF и XMP — APP1, IPTC — APP13,
#: C2PA — APP11 и т. п.) и комментарии несут данные о человеке и устройстве.
_JPEG_ALLOWED_APP = frozenset({0xE0, 0xE2, 0xEE})
_JPEG_SOS = 0xDA
_JPEG_EOI = b"\xff\xd9"
#: APPn или COM в сжатых данных: там 0xFF всегда идёт с 0x00 или RSTn, так что
#: такая пара — настоящий маркер (блок между проходами, вшитый следом файл).
_JPEG_METADATA_IN_SCAN = re.compile(rb"\xff[\xe0-\xef\xfe]")


def _is_clean_jpeg(data: bytes) -> bool:
    """JPEG, который можно отдать байт в байт: в заголовке из APPn — только
    разрешённые, комментариев нет; после начала сжатых данных — ни одного APPn/COM;
    файл кончается концом картинки (за ним не вшито видео «живого фото»). Иначе —
    перекодирование: оно срезает всё. Результат перекодирования чистый, поэтому
    повторный прогон даёт те же байты (id картинки — их хеш)."""
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xFF:  # заполнители перед настоящим маркером
            pos += 1
            continue
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        end = pos + 2 + length
        if length < 2 or end > len(data):
            return False
        if marker == _JPEG_SOS:
            tail = data[pos:]
            return tail.find(_JPEG_EOI) == len(tail) - 2 and not (
                _JPEG_METADATA_IN_SCAN.search(tail)
            )
        if marker == 0xFE or (
            0xE0 <= marker <= 0xEF and marker not in _JPEG_ALLOWED_APP
        ):
            return False
        pos = end
    return False


def _normalize_opened(img: Image, data: bytes) -> NormalizedImage:
    from PIL import Image, ImageOps

    source_format = img.format
    img.seek(0)
    img.load()
    rotated = img.getexif().get(_EXIF_ORIENTATION, 1) != 1
    img = ImageOps.exif_transpose(img)
    orig_width, orig_height = img.size
    target = _target_size(orig_width, orig_height)
    if (
        source_format == "JPEG"
        and not rotated
        and img.mode in ("RGB", "L")
        and target == img.size
        and _is_clean_jpeg(data)
    ):
        return NormalizedImage(
            data, MIME_JPEG, orig_width, orig_height, orig_width, orig_height
        )
    img = _to_rgb_or_gray(img)
    if target != img.size:
        img = img.resize(target, Image.Resampling.LANCZOS)
    encoded, mime = _encode(img)
    return NormalizedImage(
        encoded, mime, img.width, img.height, orig_width, orig_height
    )


def _target_size(width: int, height: int) -> tuple[int, int]:
    """Размер после ужатия: длинная сторона ≤ MAX_LONG_EDGE, короткая ≥ MIN_SHORT_EDGE.

    Пропорции сохраняются. Для полос длиннее ``MAX_LONG_EDGE / MIN_SHORT_EDGE``
    (≈ 49:1) оба правила сразу не выполнить — такая картинка сжимается по длинной
    стороне с искажением пропорций: крошечная сторона даёт 400 у части моделей,
    а это хуже искажения.
    """
    scale = min(1.0, MAX_LONG_EDGE / max(width, height))
    if min(width, height) * scale < MIN_SHORT_EDGE:
        scale = MIN_SHORT_EDGE / min(width, height)
    return (
        min(MAX_LONG_EDGE, max(MIN_SHORT_EDGE, round(width * scale))),
        min(MAX_LONG_EDGE, max(MIN_SHORT_EDGE, round(height * scale))),
    )


def _to_rgb_or_gray(img: Image) -> Image:
    """Режим пикселей → ``RGB`` или ``L`` (8 бит)."""
    mode = img.mode
    if mode in ("RGB", "L"):
        return img
    if mode == "1":
        return img.convert("L")
    if mode.startswith("I"):
        return _to_8bit_gray(img)
    if mode in _ALPHA_MODES or (mode == "P" and "transparency" in img.info):
        return _flatten_alpha(img)
    return img.convert("RGB")


def _to_8bit_gray(img: Image) -> Image:
    """16/32-битные оттенки серого → 8 бит масштабом, а не обрезкой по 255."""
    wide = img.convert("I")
    if wide.getextrema()[1] > 255:
        wide = wide.point(lambda value: value * (1 / 256))
    return wide.convert("L")


def _flatten_alpha(img: Image) -> Image:
    """Прозрачность → заливка белым. Альфа, равная нулю целиком, игнорируется:
    так приходят DIBV5 из буфера Windows, и заливка дала бы белый лист."""
    from PIL import Image

    rgba = img.convert("RGBA")
    alpha = rgba.getchannel("A")
    if alpha.getextrema() == (0, 0):
        return rgba.convert("RGB")
    flat = Image.new("RGB", rgba.size, _WHITE)
    flat.paste(rgba, mask=alpha)
    return flat


def _encode(img: Image) -> tuple[bytes, str]:
    """PNG; вышло больше ``JPEG_THRESHOLD`` — JPEG q85."""
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    if buffer.tell() <= JPEG_THRESHOLD:
        return buffer.getvalue(), MIME_PNG
    buffer = BytesIO()
    img.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
    return buffer.getvalue(), MIME_JPEG
