from rich.text import Text

from startup_notices import Notice
from tui.services.notices_ui import (
    INFO_GLYPH,
    WARN_GLYPH,
    footer_left_with_badge,
    format_notices_badge,
    notice_counts,
)


def test_notice_counts_and_badge():
    notices = [Notice("warning", "a", "x"), Notice("info", "b", "y")]
    assert notice_counts(notices) == (1, 1)
    # счётчики читаются парой — точка отделяет их от подсказки, а не друг от друга
    assert format_notices_badge(notices) == f"{WARN_GLYPH}1 {INFO_GLYPH}1 · F4/notices"
    assert format_notices_badge([]) == ""


def test_glyph_width_matches_what_the_terminal_draws():
    """Значок обязан считаться за ДВЕ ячейки — столько же, сколько терминал его рисует.

    Без вариационного селектора Rich считал его за одну, и склеенное число садилось
    прямо на вторую половину значка: на экране «⚠2» превращалось в кашу (живая
    приёмка). Пробелом эту разницу больше не затыкаем — он и был точкой разрыва."""
    for glyph in (WARN_GLYPH, INFO_GLYPH):
        assert Text(glyph).cell_len == 2, f"{glyph!r} считается за {Text(glyph).cell_len}"
        assert Text(f"{glyph}7").cell_len == 3   # число встаёт ПОСЛЕ значка


def test_counter_pair_never_splits_on_wrap():
    """Глиф и его число обязаны переноситься вместе. В футере строка переносится, и
    пара рвалась: «ℹ» оставалась наверху, а «2» уезжала вниз (живая приёмка).

    Неразрывный пробел здесь не помогает — перенос Rich режет по любому Unicode-
    пробелу, включая U+00A0 (замерено); единственное надёжное средство — склейка."""
    from rich.console import Console
    from rich.text import Text

    notices = [Notice("warning", "a", "x"), Notice("info", "b", "y"),
               Notice("info", "c", "z")]
    line = footer_left_with_badge(
        "F1/help  F2/sessions  F3/diff  F5/config  F6/settings  F7/buffer",
        format_notices_badge(notices), width=0)
    for width in range(40, 100):
        rendered = [seg.plain.rstrip()
                    for seg in Text(line).wrap(Console(width=width), width)]
        for row in rendered:
            assert not row.endswith("⚠") and not row.endswith("ℹ"), (
                f"ширина {width}: счётчик оторвался от глифа\n  " + "\n  ".join(rendered)
            )


BADGE = f"{WARN_GLYPH}1 {INFO_GLYPH}2 · F4/notices"


def test_badge_sits_beside_commands_when_it_fits():
    """Широко: блок в той же строке, разделитель-точка на месте — слева есть сосед,
    и точке есть что разделять."""
    assert footer_left_with_badge("F1/help  F2/sessions", BADGE, width=80) ==         f"F1/help  F2/sessions  ·  {BADGE}"


def test_badge_moves_down_whole_and_loses_the_leading_dot():
    """Узко: блок уходит вниз ЦЕЛИКОМ и без ведущей точки — в начале строки
    разделителю нечего разделять. Частичный перенос («ℹ1 ·» наверху, «F4/notices»
    внизу) читался как два обрывка (живая приёмка)."""
    from rich.cells import cell_len
    commands = "F1/help  F2/sessions  F3/diff"
    text = footer_left_with_badge(commands, BADGE, width=cell_len(commands) + 6)
    assert text == f"{commands}\n{BADGE}"


def test_badge_join_respects_the_last_wrapped_line():
    """Команды сами перенеслись — решение о точке принимает ПОСЛЕДНЯЯ строка: рядом
    с её коротким хвостом место есть, и блок встаёт туда с точкой."""
    commands = "F1/help  F2/sessions  F3/diff  F5/config  F6/settings  F7/buffer"
    text = footer_left_with_badge(commands, BADGE, width=48)
    assert f"  ·  {BADGE}" in text          # блок пристроен к хвосту, точка на месте
    assert not text.endswith(f"\n{BADGE}")  # а не выселен на отдельную строку


def test_badge_join_without_width_keeps_the_dot():
    """Ширины ещё нет (первый кадр, layout не прошёл) — собираем оптимистично, с
    точкой: ближайший resize/refresh пересоберёт по фактическому месту."""
    assert footer_left_with_badge("F1/help", BADGE, width=0) == f"F1/help  ·  {BADGE}"


def test_badge_join_with_empty_badge_returns_commands():
    assert footer_left_with_badge("F1/help", "", width=80) == "F1/help"
