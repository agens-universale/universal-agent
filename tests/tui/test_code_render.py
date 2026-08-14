"""code_render — подсветка кода в rich.Text, пригодный для выделения мышью."""
from __future__ import annotations

from rich.text import Text

from tui.services.code_render import highlight_code, normalize_code


def test_normalize_strips_trailing_newlines():
    assert normalize_code("x = 1\n\n") == "x = 1"


def test_normalize_expands_tabs_like_lexer():
    assert normalize_code("def f():\n\tpass") == "def f():\n    pass"


def test_normalize_keeps_inner_blank_lines():
    assert normalize_code("a = 1\n\nb = 2") == "a = 1\n\nb = 2"


def test_highlight_returns_text():
    assert isinstance(highlight_code("x = 1", "python"), Text)


def test_highlight_plain_equals_normalized():
    code = "def f(x):\n    return x + 1"
    assert highlight_code(code, "python").plain == code


def test_highlight_plain_equals_normalized_with_tabs():
    code = "def f():\n\tpass"
    assert highlight_code(code, "python").plain == normalize_code(code)


def test_highlight_has_no_trailing_newline():
    assert not highlight_code("x = 1", "python").plain.endswith("\n")


def test_highlight_colors_tokens():
    colors = {str(span.style.color) for span in highlight_code("def f(): pass", "python").spans}
    assert len(colors) > 1


def test_highlight_has_no_background():
    text = highlight_code("def f(): pass", "python")
    assert str(text.style) == ""
    assert {str(span.style.bgcolor) for span in text.spans} == {"None"}


def test_highlight_unknown_language_falls_back_to_plain_text():
    code = "capacity: 1024"
    text = highlight_code(code, "nosuchlanguage")
    assert text.plain == code


def test_highlight_without_language():
    code = "capacity: 1024\nttl: 300"
    assert highlight_code(code, None).plain == code


def test_highlight_preserves_trailing_spaces():
    """Хвостовые пробелы не съедаются (rstrip запрещён)."""
    code = "x = 1   \n"
    assert highlight_code(code, "python").plain == normalize_code(code)
    assert highlight_code(code, "python").plain == "x = 1   "


def test_highlight_preserves_whitespace_only_code():
    """Код из одних пробелов не исчезает."""
    code = "   "
    assert highlight_code(code, "python").plain == "   "
    assert highlight_code(code, "python").plain == normalize_code(code)


def test_highlight_preserves_inner_trailing_spaces():
    """Хвостовые пробелы в середине многострочного кода сохраняются."""
    code = "a = 1   \nb = 2"
    assert highlight_code(code, "python").plain == normalize_code(code)
    # Убедиться, что вторая строка целая
    lines = highlight_code(code, "python").plain.split("\n")
    assert lines[0] == "a = 1   "  # первая строка с пробелами
    assert lines[1] == "b = 2"     # вторая строка


def test_highlight_spans_not_shifted_after_trim():
    """Спаны остаются на месте после срезания хвостового \\n."""
    code = "def f(): pass"
    text = highlight_code(code, "python")
    # Проверить, что подсветка есть (спанов больше одного цвета)
    colors = {str(span.style.color) for span in text.spans}
    assert len(colors) > 1
    # Все спаны в пределах plain
    for span in text.spans:
        assert span.start >= 0 and span.end <= len(text.plain)
    # plain совпадает с normalize_code
    assert text.plain == normalize_code(code)


def test_comments_use_dim_not_monokai_grey():
    """Комментарии красятся $dim: родной серый monokai (#75715e) тонет и на
    панели (контраст 3.3, на грани), и особенно поверх выделения текста."""
    from tui.palette import DIM

    text = highlight_code("def f():\n    # проверка\n    return 1", "python")
    comment = next(
        span for span in text.spans if "проверка" in text.plain[span.start:span.end]
    )
    assert comment.style.color.triplet.hex == DIM.lower()


def test_other_tokens_keep_monokai():
    """Меняем только комментарии — остальная подсветка остаётся монокаевской."""
    text = highlight_code("def f():\n    return 1", "python")
    colors = {span.style.color.triplet.hex for span in text.spans}
    assert "#66d9ef" in colors        # def
    assert "#ae81ff" in colors        # число
