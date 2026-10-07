from gc_core.startup_notices import Notice
from gc_tui.services.notices_ui import (
    INFO_GLYPH,
    WARN_GLYPH,
    footer_left_with_badge,
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


def test_counter_pair_never_splits_on_wrap():
    """Глиф и его число обязаны переноситься вместе. В футере строка переносится, и
    пара рвалась: «ℹ» оставалась наверху, а «2» уезжала вниз (живая приёмка).

    Неразрывный пробел здесь не помогает — перенос Rich режет по любому Unicode-
    пробелу, включая U+00A0 (замерено); единственное надёжное средство — склейка."""
    from rich.console import Console
    from rich.text import Text

    notices = [
        Notice("warning", "a", "x"),
        Notice("info", "b", "y"),
        Notice("info", "c", "z"),
    ]
    cmds = "F1/help  F2/sessions  F3/diff  F5/config  F6/settings  F7/buffer"
    badge = format_notices_badge(notices)
    # Ширину спрашиваем ту же, какой будем рисовать, — это и есть боевой путь:
    # сборщик сам решает, встаёт блок в строку команд или уходит вниз целиком.
    for width in range(30, 120):
        rendered = [
            seg.plain.rstrip()
            for seg in Text(
                footer_left_with_badge(cmds, badge, width=width)
            ).wrap(Console(width=width), width)
        ]
        for row in rendered:
            assert not row.endswith("⚠") and not row.endswith("ℹ"), (
                f"ширина {width}: счётчик оторвался от глифа\n  "
                + "\n  ".join(rendered)
            )


BADGE = f"{WARN_GLYPH} 1  {INFO_GLYPH} 2 · F4/notices"


def test_badge_sits_beside_commands_when_it_fits():
    """Широко: блок в той же строке, разделитель-точка на месте — слева есть сосед,
    и точке есть что разделять."""
    assert (
        footer_left_with_badge("F1/help  F2/sessions", BADGE, width=80)
        == f"F1/help  F2/sessions  ·  {BADGE}"
    )


def test_badge_moves_down_whole_and_loses_the_leading_dot():
    """Узко: блок уходит вниз ЦЕЛИКОМ и без ведущей точки — в начале строки
    разделителю нечего разделять. Частичный перенос («ℹ1 ·» наверху, «F4/notices»
    внизу) читался как два обрывка (живая приёмка)."""
    from rich.cells import cell_len

    commands = "F1/help  F2/sessions  F3/diff"
    text = footer_left_with_badge(
        commands, BADGE, width=cell_len(commands) + 6
    )
    assert text == f"{commands}\n{BADGE}"


def test_badge_join_respects_the_last_wrapped_line():
    """Команды сами перенеслись — решение о точке принимает ПОСЛЕДНЯЯ строка: рядом
    с её коротким хвостом место есть, и блок встаёт туда с точкой."""
    commands = (
        "F1/help  F2/sessions  F3/diff  F5/config  F6/settings  F7/buffer"
    )
    text = footer_left_with_badge(commands, BADGE, width=48)
    assert f"  ·  {BADGE}" in text  # блок пристроен к хвосту, точка на месте
    assert not text.endswith(f"\n{BADGE}")  # а не выселен на отдельную строку


def test_badge_join_without_width_keeps_the_dot():
    """Ширины ещё нет (первый кадр, layout не прошёл) — собираем оптимистично, с
    точкой: ближайший resize/refresh пересоберёт по фактическому месту."""
    assert (
        footer_left_with_badge("F1/help", BADGE, width=0)
        == f"F1/help  ·  {BADGE}"
    )


def test_badge_join_with_empty_badge_returns_commands():
    assert footer_left_with_badge("F1/help", "", width=80) == "F1/help"
