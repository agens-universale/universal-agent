from gc_core.startup_notices import Notice
from gc_tui.services.notices_ui import (
    INFO_GLYPH,
    WARN_GLYPH,
    format_notices_badge,
    notice_counts,
)
from rich.text import Text


def test_notice_counts_and_badge():
    notices = [Notice("warning", "a", "x"), Notice("info", "b", "y")]
    assert notice_counts(notices) == (1, 1)
    # счётчики читаются парой — точка отделяет их от подсказки, а не друг от друга;
    # внутри пары пробел, между парами двойной: иначе «⚠ 1 ℹ 1» читается как четыре
    # отдельных знака, а не как два счётчика
    assert (
        format_notices_badge(notices)
        == f"{WARN_GLYPH} 1  {INFO_GLYPH} 1 · F4/notices"
    )
    assert format_notices_badge([]) == ""


def test_glyphs_do_not_bet_on_how_wide_the_terminal_draws_them():
    """Значки — БЕЗ вариационного селектора, и число отделено пробелом.

    Ставка на ширину значка невозможна в принципе: Windows рисует голый «⚠» в две
    ячейки, macOS — в одну, а с селектором U+FE0F macOS рисует цветную картинку,
    наползающую на соседнюю клетку. Мы пробовали селектор (Rich считает такую пару
    за 2) — на Windows сошлось, на macOS приклеенное вплотную число ушло под
    картинку (отчёт коллеги 2026-08-18).

    Работает не «правильный значок», а ПРОБЕЛ после него: нарисуется значок шире —
    съест пробел, и ничего не спрячется. Так устроены остальные 45 мест в проекте
    («⚠ MCP: 0/1 …», «⚠ не удалось проверить…»), и они на обеих системах целы."""
    for glyph in (WARN_GLYPH, INFO_GLYPH):
        assert "️" not in glyph, f"{glyph!r}: вернулся селектор эмодзи U+FE0F"
        assert "︎" not in glyph, (
            f"{glyph!r}: селектор U+FE0E терминал вправе игнорировать"
        )
        assert len(glyph) == 1, f"{glyph!r}: значок обязан быть одним символом"
        assert Text(glyph).cell_len == 1
    badge = format_notices_badge(
        [Notice("warning", "a", "x")], with_hint=False
    )
    assert badge == f"{WARN_GLYPH} 1"  # число ОТДЕЛЕНО от значка
