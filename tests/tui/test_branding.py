"""branding — единый источник бренд-контента TUI GIGACODE."""
from gc_tui import branding


def test_logo_shape():
    lines = branding.LOGO.split("\n")
    assert len(lines) == 6
    assert max(len(line) for line in lines) == 62


def test_gradient_color_stops():
    # амбер (RUN_COLOR) -> олива (OLIVE) -> teal (ACCENT)
    assert branding.gradient_color(0.0) == "#D4A574"
    assert branding.gradient_color(0.5) == "#8FA05A"
    assert branding.gradient_color(1.0) == "#54B3A6"


def test_gradient_color_clamps():
    assert branding.gradient_color(-1.0) == "#D4A574"
    assert branding.gradient_color(2.0) == "#54B3A6"


def test_logo_text_plain_matches_and_spaces_unstyled():
    t = branding.logo_text()
    assert t.plain == branding.LOGO
    # каждый видимый глиф получает свой span; пробелы и переводы строк — нет.
    glyphs = sum(1 for ch in branding.LOGO if ch not in (" ", "\n"))
    assert len(t.spans) == glyphs


def test_tagline_display():
    assert branding.tagline_display() == "U N I V E R S A L   A G E N T"
