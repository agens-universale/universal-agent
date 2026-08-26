"""Подсветка кода для трейса: rich.Text вместо rich.Syntax.

Контракт селектируемости (см. services/json_payload.py): Widget.get_selection
отдаёт текст только для Text/Content — Syntax даёт None, и Textual молча
исключает виджет из мышиного выделения. Поэтому код едет в трейс как Text
со стилями по токенам: подсветка сохраняется, выделение работает нативно.

Владелец «как код выглядит в трейсе». Экран события (json_payload) имеет свой
путь и здесь не участвует.
"""
from __future__ import annotations

from rich.style import Style
from rich.text import Text

from gc_tui.palette import CODE_BLOCK, DIM

_TAB_WIDTH = 4
_theme = None


def _trace_theme():
    """monokai, но комментарии — цветом служебного текста проекта ($dim).

    Родные комментарии monokai (#75715e) слишком тусклы: даже на обычном фоне
    панели их контраст 3.3 — на грани, а поверх выделения текста они пропадают
    совсем. $dim читается и там, и там, и заодно роднит комментарии с остальным
    служебным текстом интерфейса. Тема строится один раз — разбор стиля
    pygments недёшев, а подсветка зовётся на каждый блок трейса.
    """
    global _theme
    if _theme is None:
        from pygments.token import Comment
        from rich.syntax import PygmentsSyntaxTheme

        class _TraceTheme(PygmentsSyntaxTheme):
            def get_style_for_token(self, token_type):
                style = super().get_style_for_token(token_type)
                if token_type in Comment:
                    return style + Style(color=DIM)
                return style

        _theme = _TraceTheme("monokai")
    return _theme


def normalize_code(content: str) -> str:
    """Текст, который реально показывается И копируется.

    Приведение к тому, что делает лексер Syntax (tabsize=4), плюс срез хвостовых
    переводов строки (ensurenl=True добавляет свой). Без этого drag и кнопка
    отдавали бы разное на коде с табами, а блок рос бы на пустую строку.
    """
    return content.replace("\t", " " * _TAB_WIDTH).rstrip("\n")


def _without_background(style: Style) -> Style:
    """Стиль токена без фона темы — фон панели задаёт CSS.

    Фон монокая мешает подсветке фокуса и выделения: он записан в стиль текста,
    и CSS его не перекрывает.
    """
    return Style(
        color=style.color,
        bold=style.bold,
        italic=style.italic,
        underline=style.underline,
        dim=style.dim,
    )


def highlight_code(content: str, lang: str | None) -> Text:
    """Код как Text: подсветка по токенам, без фона, `.plain` == normalize_code(content).

    Неизвестный язык или его отсутствие — ровный текст цветом $code-block.
    """
    code = normalize_code(content)
    if not lang:
        return Text(code, style=CODE_BLOCK)

    from rich.syntax import Syntax

    syntax = Syntax(code, lexer=lang, theme=_trace_theme(), word_wrap=False)
    if syntax.lexer is None:
        # Неизвестный язык: `Syntax` ничего не бросает — он молча оставляет текст как
        # есть и красит его цветом темы. Прежняя ловля `ClassNotFound` была
        # недостижима, и fence с mermaid выходил белым из monokai вместо $code-block.
        return Text(code, style=CODE_BLOCK)
    text = syntax.highlight(code)

    # Срезать ровно один хвостовой \n, добавленный лексером (ensurenl=True).
    # Важно: не использовать text.rstrip(), он съест любые пробельные символы,
    # нарушив контракт .plain == normalize_code(content).
    if text.plain.endswith("\n"):
        new_plain = text.plain[:-1]
    else:
        new_plain = text.plain

    # Построить новый Text с правильным plain и спанами без фона
    new_text = Text(new_plain)
    for span in text.spans:
        # Пропустить спаны, полностью выходящие за границу нового текста
        if span.start >= len(new_plain):
            continue
        # Обрезать конец спана, если он выходит за границу
        new_end = min(span.end, len(new_plain))
        new_text.stylize(_without_background(span.style), span.start, new_end)

    return new_text
