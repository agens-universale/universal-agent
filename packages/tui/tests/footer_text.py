"""Текст левой части главного футера — наблюдатель для тестов.

Левая часть — две строки: `#footer-mode` (режим и хвост команд) и
`#footer-slot` (временное и бейдж уведомлений). Прод-коду склейка не нужна,
поэтому она живёт здесь."""

from __future__ import annotations


def footer_rows(scr) -> tuple[str, str]:
    """Строка 1 и строка 2 левой части (`scr` — экран или приложение)."""
    mode = str(scr.query_one("#footer-mode").render())
    slot = str(scr.query_one("#footer-slot").render())
    return mode, slot


def footer_left(scr) -> str:
    """Обе строки через перенос; пустой слот не добавляет строки."""
    mode, slot = footer_rows(scr)
    return f"{mode}\n{slot}" if slot else mode
