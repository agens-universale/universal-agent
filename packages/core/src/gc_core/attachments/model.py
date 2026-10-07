"""Вложение-изображение: модель записи, подписи плашек, размер для записок.

Вложение — ссылка на нормализованный файл, а не байты: в историю
(``data_*.jsonl``), логи и снимки state уходит только плоский словарь
``Attachment.to_dict()``. Байты читает ``ImageStore.read_for_send`` в момент
сборки запроса.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, fields
from typing import Any, Mapping

MAX_LONG_EDGE = 1568
MIN_SHORT_EDGE = 32
JPEG_THRESHOLD = (
    1_500_000  # байт PNG после ужатия, выше — перекодировать в JPEG q85
)
MAX_INPUT_BYTES = 50 * 1024 * 1024  # до декодирования
MAX_PIXELS = 50_000_000  # w*h до load()
CONTEXT_CHARS_PER_IMAGE = 4000  # вес вашей картинки в символьных счётчиках
# Вес снимка агента: пикселями он живёт только текущий ход, дальше модели уходит
# записка — её и считаем (счётчики копятся по мере записи, живость им не видна).
CONTEXT_CHARS_PER_NOTE = 200
MAX_REQUEST_IMAGE_BYTES = (
    8 * 1024 * 1024
)  # сумма base64 живых картинок на запрос
LIVE_USER_TURNS = (
    10  # окно живости пользовательских картинок, в ходах (включая текущий)
)
ORIGIN_USER = "user"
ORIGIN_TOOL = "tool"
#: ``Attachment.source`` картинки из буфера обмена (у файла — его полный путь).
CLIPBOARD_SOURCE = "clipboard"
KIND_IMAGE = "image"
#: Форматы, которые нормализация соглашается открыть (имена форматов Pillow).
#: EPS и WMF не входят сознательно: первый зовёт Ghostscript, второй на Windows
#: рисует система.
ALLOWED_FORMATS = ("PNG", "JPEG", "GIF", "WEBP", "BMP", "TIFF")

#: Подпись снимка, сделанного тулом просмотра (модель видит её в записках).
TOOL_LABEL = "Image from fs_view_image"

_INT_FIELDS = frozenset(
    {"width", "height", "size", "orig_width", "orig_height"}
)
_TRAILING_NUMBER = re.compile(r"(\d+)$")


@dataclass(frozen=True)
class Attachment:
    id: str  # первые 16 hex sha256 нормализованных байт
    kind: str  # всегда KIND_IMAGE
    mime: str  # "image/png" | "image/jpeg"
    width: int
    height: int
    size: int  # байт нормализованного файла
    path: str  # абсолютный путь к нормализованному файлу
    origin: str  # ORIGIN_USER | ORIGIN_TOOL
    label: str  # user_label(n) или TOOL_LABEL
    source: str  # полный путь исходного файла | CLIPBOARD_SOURCE | путь, переданный тулу
    orig_width: int
    orig_height: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Attachment:
        """Строгий разбор записи: нет поля — ``KeyError``, не тот тип — ``TypeError``.

        Лишние ключи игнорируются. Тип сверяется точно: ``True`` за ``int`` не
        сойдёт, ``None`` за строку — тоже.
        """
        values: dict[str, Any] = {}
        for field in fields(cls):
            value = data[field.name]
            expected = int if field.name in _INT_FIELDS else str
            if type(value) is not expected:
                raise TypeError(
                    f"attachment.{field.name}: ожидался {expected.__name__}, "
                    f"получен {type(value).__name__}"
                )
            values[field.name] = value
        return cls(**values)


def user_label(n: int) -> str:
    """Подпись пользовательской картинки с номером ``n`` (сквозной по треду)."""
    return f"Изображение #{n}"


def label_number(label: str) -> int | None:
    """Номер ``n`` из подписи ``user_label(n)``; чужая подпись — ``None``."""
    match = _TRAILING_NUMBER.search(label)
    if match is None:
        return None
    number = int(match.group(1))
    return number if user_label(number) == label else None


def chip_text(n: int) -> str:
    """Плашка пользовательской картинки ``n`` в тексте сообщения."""
    return _chip(user_label(n))


def chip_of(att: Attachment) -> str:
    """Плашка вложения в тексте сообщения: ``[<label>]``; у пользовательской
    картинки — ``chip_text(n)``."""
    return _chip(att.label)


def _chip(label: str) -> str:
    return f"[{label}]"


def attachments_of(message: Mapping[str, Any]) -> list[Attachment]:
    """Вложения сообщения. Ключа нет или он не список — пусто; битые записи
    пропускаются: одна испорченная запись не роняет сборку запроса и restore."""
    raw = message.get("attachments")
    if not isinstance(raw, list):
        return []
    result: list[Attachment] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        try:
            result.append(Attachment.from_dict(item))
        except (KeyError, TypeError):
            continue
    return result


def wh_text(att: Attachment) -> str:
    """Формат и размер для записок: ``png 1456×819``."""
    return f"{att.mime.split('/', 1)[-1]} {att.width}×{att.height}"
