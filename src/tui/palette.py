"""Цветовые токены TUI — single source of truth для Python-рендера.

Зеркало `$vars` из `src/tui/styles.tcss` (Textual читает только TCSS, поэтому
TCSS-переменные остаются — этот модуль их дублирует для кода, который красит
через `rich.Text(..., style=...)` напрямую).

При правке значения — обновить ОБА файла. Имена констант соответствуют
TCSS-переменным (`$deep-dim` → `DEEP_DIM`, `$run-color` → `RUN_COLOR`, ...).
"""
from __future__ import annotations


# Базовые
BG = "#0E1119"
INK = "#ebe9e6"
BRIGHT = "#eef2f6"
DIM = "#8b949e"
MUTED = "#6e7681"
DEEP_DIM = "#7a8290"

# Семантические
ACCENT = "#54B3A6"
CODE = "#afbdff"
TOOL_NAME = "#BCC3CC"
CODE_BLOCK = "#9aa4b2"
OK = "#5FAE7F"
RUN_COLOR = "#D4A574"
OLIVE = "#8FA05A"          # средний стоп градиента логотипа (амбер→олива→teal)
ERR = "#C75C54"
WARN = "#f0883e"           # предупреждение «контекст почти полон» (>90%); НЕ ошибка
BRAND = "#9A6DD7"

# Поверхности
BAND_BG = "#1f2128"
FOCUS_BG = "#2a2d36"       # подсветка строки трассы в фокусе (только CSS :focus)
FRAME_ARCHIVE = "#4b525d"  # серая рамка «архив/история» (отвеченный вопрос)
