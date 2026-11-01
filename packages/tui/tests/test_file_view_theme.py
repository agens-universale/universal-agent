"""Тема подсветки `/view` — только цвета палитры (дизайн-код §1)."""

from gc_tui import palette
from gc_tui.services.file_view import _PYGMENTS_TO_TA_STYLE
from gc_tui.services.file_view_theme import file_view_theme


def _palette_colors() -> set[str]:
    return {
        value.lower()
        for name, value in vars(palette).items()
        if name.isupper() and isinstance(value, str) and value.startswith("#")
    }


def test_every_color_of_the_theme_is_a_palette_token():
    theme = file_view_theme()
    styles = [
        theme.base_style,
        theme.cursor_style,
        theme.selection_style,
        theme.gutter_style,
        *theme.syntax_styles.values(),
    ]
    allowed = _palette_colors()
    for style in styles:
        for color in (style.color, style.bgcolor):
            if color is not None:
                assert color.triplet.hex.lower() in allowed, (style, color)


def test_theme_covers_every_role_the_pygments_map_emits():
    roles = set(file_view_theme().syntax_styles)
    assert set(_PYGMENTS_TO_TA_STYLE.values()) <= roles


def test_inline_code_takes_the_code_token():
    code = file_view_theme().syntax_styles["inline_code"]
    assert code.color.triplet.hex.lower() == palette.CODE.lower()


def test_markdown_backticks_bold_and_italic_get_their_roles():
    from gc_tui.services.file_view import pygments_highlight_map

    spans = pygments_highlight_map(
        "текст `код` и **жир** и *кур*", lexer="markdown"
    )
    roles = {name for line in spans.values() for _, _, name in line}
    assert {"inline_code", "bold", "italic"} <= roles
