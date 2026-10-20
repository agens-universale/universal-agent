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


def slot_texts() -> list[str]:
    """Все тексты реестра `footer_slot`: строки, значения словарей и
    кортежей (шаблоны — как есть, без подстановки)."""
    from gc_tui.services import footer_slot

    texts: list[str] = []
    for name, value in vars(footer_slot).items():
        if name.startswith("_") or not name.isupper():
            continue
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, dict):
            texts.extend(v for v in value.values() if isinstance(v, str))
        elif isinstance(value, tuple):
            texts.extend(v for v in value if isinstance(v, str))
    return texts
