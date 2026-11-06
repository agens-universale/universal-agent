"""Тема подсветки `/view` из токенов палитры (дизайн-код §1).

Встроенная `vscode_dark` красила просмотр файла чужими цветами — экран
выглядел другим приложением. Роли те же, что у встроенных тем Textual;
цвета — палитры: код и ключи `CODE`, ключевые слова `ACCENT`, строки `OK`,
числа и константы `RUN_COLOR`, служебные знаки и комментарии приглушены.

Тема собирается при открытии экрана, а не при импорте: палитра темы `auto`
пересчитывается от фона терминала до старта приложения."""

from __future__ import annotations

from rich.style import Style
from textual.widgets.text_area import TextAreaTheme

from gc_tui import palette

FILE_VIEW_THEME = "gc-ua"


def _roles() -> dict[str, Style]:
    p = palette
    code = Style(color=p.CODE)
    keyword = Style(color=p.ACCENT)
    string = Style(color=p.OK)
    constant = Style(color=p.RUN_COLOR)
    type_ = Style(color=p.OLIVE)
    quiet = Style(color=p.DIM)
    roles = {
        "heading": Style(color=p.INK, bold=True),
        "heading.marker": Style(color=p.DEEP_DIM),
        "bold": Style(bold=True),
        "italic": Style(italic=True),
        "strikethrough": Style(strike=True),
        "link.label": code,
        "link.uri": Style(color=p.DIM, underline=True),
        "list.marker": quiet,
        "comment": Style(color=p.DEEP_DIM, italic=True),
        "operator": quiet,
        "punctuation.bracket": quiet,
        "punctuation.delimiter": quiet,
        "punctuation.special": quiet,
    }
    for name in (
        "inline_code",
        "info_string",
        "function",
        "function.call",
        "method",
        "method.call",
        "constructor",
        "json.label",
        "yaml.field",
        "css.property",
        "tag",
    ):
        roles[name] = code
    for name in (
        "keyword",
        "keyword.function",
        "keyword.operator",
        "keyword.return",
        "conditional",
        "repeat",
        "include",
        "exception",
    ):
        roles[name] = keyword
    for name in ("string", "string.documentation"):
        roles[name] = string
    for name in (
        "number",
        "float",
        "boolean",
        "constant.builtin",
        "json.null",
        "toml.datetime",
    ):
        roles[name] = constant
    for name in ("type", "type.builtin", "type.class", "class", "toml.type"):
        roles[name] = type_
    return roles


def file_view_theme() -> TextAreaTheme:
    """Тема области текста `/view` по текущей палитре."""
    return TextAreaTheme(
        name=FILE_VIEW_THEME,
        base_style=Style(color=palette.INK, bgcolor=palette.BG),
        gutter_style=Style(color=palette.DIM),
        cursor_style=Style(color=palette.BG, bgcolor=palette.INK),
        selection_style=Style(bgcolor=palette.SCREEN_SELECTION_BACKGROUND),
        syntax_styles=_roles(),
    )
