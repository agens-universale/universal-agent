"""Открыть картинку просмотрщиком ОС: клик по картинке в ленте, Ctrl+клик по плашке в поле
ввода (дизайн картинок §20.2). Общая точка экранов с лентой: главный показывает отказ в
футере, превью сессии — уведомлением."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from gc_tui.services import os_ops

FILE_UNAVAILABLE_REFUSAL = "✗ файл недоступен"
VIEWER_FAILED_REFUSAL = "✗ не удалось открыть просмотрщик изображений"


def open_image(path: str) -> Optional[str]:
    """Открыть файл; текст отказа или None. Наличие файла проверяется здесь: просмотрщик
    ОС может запуститься и без файла и вернуть успех. Файла нет и просмотрщик не
    запустился — разные отказы (причина запуска — в логе `os_ops.open_path`)."""
    if not Path(path).is_file():
        return FILE_UNAVAILABLE_REFUSAL
    if not os_ops.open_path(path):
        return VIEWER_FAILED_REFUSAL
    return None
